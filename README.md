# GoFormz Extractor — Azure Container Apps (Scheduled Job)

Production build that logs into GoFormz, enumerates forms, extracts the
created/report date (Form Name → DOM → Azure Document Intelligence OCR), and
uploads a CSV to a Fabric Lakehouse (OneLake). Runs headless in a container
every **Friday 07:00 UTC**.

## Files

| File | Purpose |
|------|---------|
| `formsDataDownload.py` | The extractor. All secrets/config come from env vars. |
| `Dockerfile` | Python 3.11 image **with Chromium + chromedriver**, runs as non-root. |
| `requirements.txt` | Pinned Python dependencies. |
| `deploy.sh` | Build → push to ACR → create/update a **scheduled Job** (managed identity, immutable digest). |
| `.env.example` | Template for local testing (copy to `.env`). |
| `.dockerignore` | Keeps secrets, data files, and docs out of the image. |

## Security model

- **No secrets in code.** `formsDataDownload.py` reads everything from
  environment variables and fails fast if a required one is missing.
- **Container App secrets** hold the GoFormz password, DI key, and Fabric
  client secret; env vars reference them via `secretref:`.
- **No ACR admin account.** The Job pulls its image using a user-assigned
  **managed identity** granted only `AcrPull`.
- **Immutable image digest** is deployed (not `:latest`) for reproducibility
  and clean rollback.

> ⚠️ If the old script (with hardcoded GoFormz password + DI key) was ever
> committed or shared, **rotate both credentials now** and purge them from
> git history and any built images.

## Quick start

```bash
# 1. Provide secrets in your shell (or CI / Key Vault)
export GOFORMZ_EMAIL="umella@verdantas.com"
export GOFORMZ_PASSWORD="********"
export AZURE_DI_ENDPOINT="https://<your-di>.cognitiveservices.azure.com/"
export AZURE_DI_KEY="********"
export TENANT_ID="********"
export CLIENT_ID="********"
export CLIENT_SECRET="********"
export FABRIC_WORKSPACE_NAME="vdt-dev-fbrc-wkspc"
export FABRIC_LAKEHOUSE_NAME="Ashmit_Test_Lakehouse"

# 2. Deploy
az login
bash deploy.sh

# 3. Manual test run
az containerapp job start -n goformz-extractor -g rg-goformz

# 4. Watch executions
az containerapp job execution list -n goformz-extractor -g rg-goformz -o table
```

## Local testing

```bash
cp .env.example .env      # fill in real values
# optional: run against disk instead of OneLake
#   set REQUIRE_ONELAKE_UPLOAD=false in .env
pip install -r requirements.txt
python formsDataDownload.py
```

## Grant the service principal OneLake access

The `CLIENT_ID` service principal must have write access to the target Fabric
workspace/Lakehouse. In Fabric, add the app (or a security group it belongs to)
as a **Contributor/Member** on the workspace, or grant item-level write on the
Lakehouse. Without this, the upload — and therefore the Job — will fail by
design (no silent success).

## Behaviour notes

- **Date range:** leave `START_DATE`/`END_DATE` empty and the job processes the
  **completed** previous Friday→Thursday cycle. Set them (YYYY-MM-DD) for a
  manual backfill.
- **Quota-aware:** GoFormz `403 "Out of call volume quota"` is detected; the
  job waits out the window (capped by `MAX_QUOTA_WAIT_SECONDS`) and retries the
  same form.
- **Session recovery:** one controlled browser re-login is attempted if the
  copied web session expires mid-run.
- **Fail-loud:** empty results, an all-Partial/Failed batch, or a failed
  OneLake upload cause a **non-zero exit** so the Job shows **Failed**.

## App vs Job

Use the **Job** (this package) for the weekly batch — it runs on the cron
schedule, executes once, and scales to zero. Use a Container **App** only if
you need a long-running HTTP service.

## Pre-production checklist

1. Rotate any previously exposed GoFormz password + DI key.
2. Confirm the service principal can write to the Lakehouse.
3. `az containerapp job start` → verify a CSV lands in `Files/goformz`.
4. Break a credential on purpose → confirm the execution is **Failed**, not
   Succeeded.
5. Confirm logs show only summary counts (no full data dump).
