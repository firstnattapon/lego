param(
  [Parameter(Mandatory=$true)][ValidateSet('UAT','PROD')][string]$Environment,
  [Parameter(Mandatory=$true)][string]$ProjectId,
  [Parameter(Mandatory=$true)][string]$DatabaseUrl,
  [Parameter(Mandatory=$true)][string]$CandidateHash,
  [Parameter(Mandatory=$true)][string]$Symbol,
  [Parameter(Mandatory=$true)][decimal]$PrincipalUsd,
  [Parameter(Mandatory=$true)][decimal]$DiffUsd,
  [Parameter(Mandatory=$true)][string]$DnaBundle,
  [string]$Region = 'asia-southeast1',
  [string]$Mode = 'observe',
  [bool]$Active = $false,
  [string]$ReleaseAuthorization = '',
  [string]$TokenSecret = ''
)
$ErrorActionPreference = 'Stop'
if (-not $DatabaseUrl.StartsWith('https://')) { throw 'DatabaseUrl must be HTTPS' }
if ($Mode -notin @('observe','trade')) { throw 'Mode must be observe or trade' }
$manifestJson = & python tools/candidate_manifest.py
if ($LASTEXITCODE -ne 0) { throw 'Cannot compute source candidate manifest' }
$manifest = $manifestJson | ConvertFrom-Json
if ($CandidateHash -ne $manifest.candidate_hash) {
  throw 'CandidateHash differs from this source tree; regenerate release evidence and binding'
}
$suffix = $Environment.ToLowerInvariant()
$functionName = "lego-tick-$suffix"
$serviceAccount = "lego-runtime-$suffix@$ProjectId.iam.gserviceaccount.com"
$envVars = @(
  "WEBULL_ENV=$Environment", "FIREBASE_DB_URL=$DatabaseUrl",
  "LEGO_SYMBOL=$Symbol", "LEGO_FIX_C=$PrincipalUsd", "LEGO_DIFF=$DiffUsd",
  "LEGO_DNA_BUNDLE=$DnaBundle", "LEGO_MODE=$Mode",
  "LEGO_ACTIVE=$($Active.ToString().ToLowerInvariant())",
  "LEGO_CANDIDATE_HASH=$CandidateHash",
  "LEGO_RELEASE_AUTHORIZATION=$ReleaseAuthorization",
  'WEBULL_TOKEN_DIR=/tmp/webull_token',
  'LEGO_DNA_CLOCK_MODE=market'
)
# Some UAT applications authenticate with HMAC and never create a token file.
# Binding an empty secret container would make every cold start fail before the
# SDK can prove that. Hydration is explicit and requires an enabled version.
if ($TokenSecret) {
  if ($TokenSecret -notmatch '^projects/([^/]+)/secrets/([^/]+)$') {
    throw 'TokenSecret must be projects/PROJECT/secrets/NAME'
  }
  $tokenProject = $Matches[1]
  $tokenName = $Matches[2]
  $tokenState = (& gcloud secrets versions describe latest `
    "--secret=$tokenName" "--project=$tokenProject" `
    --format='value(state)' 2>$null).Trim()
  if ($LASTEXITCODE -ne 0 -or $tokenState -ne 'ENABLED') {
    throw "TokenSecret latest version is missing or not ENABLED: $TokenSecret"
  }
  $envVars += "WEBULL_TOKEN_SECRET=$TokenSecret"
}
$envVars = $envVars -join ','
$secrets = "WEBULL_APP_KEY=webull-app-key-$suffix`:latest,WEBULL_APP_SECRET=webull-app-secret-$suffix`:latest,WEBULL_ACCOUNT_ID=webull-account-id-$suffix`:latest"
& gcloud functions deploy $functionName --gen2 --runtime=python312 --region=$Region `
  --source=. --entry-point=lego_tick --trigger-http --no-allow-unauthenticated `
  --service-account=$serviceAccount --memory=512Mi --timeout=45s `
  --concurrency=1 --max-instances=1 --min-instances=0 `
  --set-env-vars=$envVars --set-secrets=$secrets --project=$ProjectId
if ($LASTEXITCODE -ne 0) { throw "gcloud deploy failed: $LASTEXITCODE" }
& gcloud functions describe $functionName --gen2 --region=$Region --project=$ProjectId `
  --format='json(name,serviceConfig.uri,serviceConfig.revision,updateTime,state)'

