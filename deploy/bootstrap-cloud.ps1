param(
  [Parameter(Mandatory=$true)][ValidateSet('UAT','PROD')][string]$Environment,
  [Parameter(Mandatory=$true)][string]$ProjectId,
  [string]$Region = 'asia-southeast1',
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$suffix = $Environment.ToLowerInvariant()
$runtimeName = "lego-runtime-$suffix"
$schedulerName = "lego-scheduler-$suffix"
$runtimeSa = "$runtimeName@$ProjectId.iam.gserviceaccount.com"
$secretNames = @("webull-app-key-$suffix", "webull-app-secret-$suffix", "webull-account-id-$suffix", "webull-token-$suffix")

function Run([string[]]$Arguments) {
  if (-not $Apply) { Write-Output ("gcloud " + ($Arguments -join ' ')); return }
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) { throw "gcloud failed ($LASTEXITCODE): $($Arguments -join ' ')" }
}
function Ensure([string[]]$TestArguments, [string[]]$CreateArguments) {
  if (-not $Apply) { Write-Output ("gcloud " + ($CreateArguments -join ' ')); return }
  & gcloud @TestArguments *> $null
  if ($LASTEXITCODE -ne 0) { Run $CreateArguments }
}

# Create only identity/resource containers. Secret values are deliberately never
# accepted by this script; add versions through ops.py bootstrap-auth or a secure
# administrator workflow.
Ensure @('iam','service-accounts','describe',$runtimeSa,"--project=$ProjectId") @('iam','service-accounts','create',$runtimeName,"--project=$ProjectId",'--display-name=LEGO runtime')
Ensure @('iam','service-accounts','describe',"$schedulerName@$ProjectId.iam.gserviceaccount.com","--project=$ProjectId") @('iam','service-accounts','create',$schedulerName,"--project=$ProjectId",'--display-name=LEGO scheduler')
foreach ($name in $secretNames) {
  Ensure @('secrets','describe',$name,"--project=$ProjectId") @('secrets','create',$name,'--replication-policy=automatic',"--project=$ProjectId")
  Run @('secrets','add-iam-policy-binding',$name,"--member=serviceAccount:$runtimeSa",'--role=roles/secretmanager.secretAccessor',"--project=$ProjectId")
}
Run @('projects','add-iam-policy-binding',$ProjectId,"--member=serviceAccount:$runtimeSa",'--role=roles/firebasedatabase.admin')
Run @('projects','add-iam-policy-binding',$ProjectId,"--member=serviceAccount:$runtimeSa",'--role=roles/logging.logWriter')

if (-not $Apply) {
  Write-Output 'DRY RUN ONLY. Re-run with -Apply after reviewing project/environment and authenticating gcloud.'
}

