param(
  [Parameter(Mandatory=$true)][ValidateSet('UAT','PROD')][string]$Environment,
  [Parameter(Mandatory=$true)][string]$ProjectId,
  [string]$Region = 'asia-southeast1',
  [string]$Schedule = '* * * * *',
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$suffix = $Environment.ToLowerInvariant()
$functionName = "lego-tick-$suffix"
$jobName = "lego-tick-$suffix"
$schedulerSa = "lego-scheduler-$suffix@$ProjectId.iam.gserviceaccount.com"

if (-not $Apply) {
  Write-Output "DRY RUN: resolve URI for $functionName in $ProjectId/$Region"
  Write-Output "Then grant only $schedulerSa invoker and create/update Scheduler $jobName with audience equal to that exact URI."
  exit 0
}

$uri = (& gcloud functions describe $functionName --gen2 "--region=$Region" "--project=$ProjectId" --format='value(serviceConfig.uri)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $uri.StartsWith('https://')) { throw 'Cannot resolve deployed function HTTPS URI' }
& gcloud functions add-invoker-policy-binding $functionName --gen2 "--region=$Region" "--project=$ProjectId" "--member=serviceAccount:$schedulerSa"
if ($LASTEXITCODE -ne 0) { throw 'Failed to bind function invoker' }

& gcloud scheduler jobs describe $jobName "--location=$Region" "--project=$ProjectId" *> $null
$verb = if ($LASTEXITCODE -eq 0) { 'update' } else { 'create' }
& gcloud scheduler jobs $verb http $jobName "--location=$Region" "--project=$ProjectId" "--schedule=$Schedule" '--time-zone=UTC' "--uri=$uri" '--http-method=POST' "--oidc-service-account-email=$schedulerSa" "--oidc-token-audience=$uri" '--max-retry-attempts=0'
if ($LASTEXITCODE -ne 0) { throw "Failed to $verb Scheduler job" }

Write-Output "Configured $jobName -> $uri with OIDC audience $uri"
