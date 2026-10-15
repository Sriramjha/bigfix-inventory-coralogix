# BigFix inventory ? Coralogix

Pull **BigFix computer inventory** once a day and ship one log per computer to **Coralogix**.

Each run evaluates one Session Relevance query on the BigFix root server (`POST /api/query`) and ingests the rows with the [Coralogix REST API](https://coralogix.com/docs/developer-portal/apis/log-ingestion/coralogix-rest-api-singles/) (`POST https://ingress.<domain>/logs/v1/singles`).

The log timestamp is the time of the pull, so the whole snapshot appears under **Last 24 hours**. `last_report_time` stays inside the event. A computer that has not reported within `BIGFIX_STALE_DAYS` (default 7) is still shipped, at Warning severity.

BigFix REST API reference: <https://developer.bigfix.com/rest-api/>

---

## Architecture

```text
????????????????????????      once a day (cron)      ??????????????????????????????
? cron + run_daily.sh  ? ??????????????????????????? ? bigfix_inventory_to_       ?
????????????????????????                             ? coralogix.py               ?
                                                     ??????????????????????????????
                                                                   ?
                    ???????????????????????????????????????????????????????????????????????????
                    ?                                                                         ?
     POST /api/query  (Session Relevance, HTTP Basic)                        POST ingress.<domain>/logs/v1/singles
     BigFix root server :52311                                               Coralogix Send-Your-Data key
```

---

## Prerequisites

| Component | Requirement |
|-----------|-------------|
| **BigFix** | Root server reachable on HTTPS, default port **52311** |
| **Operator** | A console operator that can sign in to the REST API and can see the computers to export. A Master Operator sees every computer. |
| **Coralogix** | A **Send Your Data** API key and the account domain |
| **Runtime** | Python 3.10+, Linux or macOS host for cron |
| **Network** | Outbound HTTPS from that host to the BigFix root server and to `ingress.<your-domain>` |

---

## Implementation steps

### Step 1 — Clone and install

```bash
git clone https://github.com/Sriramjha/bigfix-inventory-coralogix.git
cd bigfix-inventory-coralogix

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

chmod +x scripts/run_daily.sh
mkdir -p logs
```

Confirm the parser without any credentials:

```bash
python bigfix_inventory_to_coralogix.py --self-test
```

Expected output: `self-test ok`

### Step 2 — Create the BigFix operator

1. In the BigFix console, create a dedicated operator for this export (or choose an existing one).
2. Give that operator visibility of the computers you want in Coralogix. A non-master operator only returns the computers it administers.
3. Confirm the REST API answers. From a machine that can reach the root server:

```bash
curl -k -u 'OPERATOR:PASSWORD' "https://BIGFIX_HOST:52311/api/help"
```

A short HTML or XML help page means the account can use the API. `401` means the username or password is wrong.

The root server URL is `https://<host>:52311`. If your deployment uses another port, include it in `BIGFIX_URL`.

### Step 3 — Create the Coralogix key

1. Open the Coralogix console ? **Data Flow** ? **API Keys**.
2. Create or copy a **Send Your Data** key. A query key will not ingest logs.
3. Set `CORALOGIX_DOMAIN` from the console URL. Take the host **after** `.app.` and drop `https://`.

| Console URL | `CORALOGIX_DOMAIN` |
|-------------|--------------------|
| `https://<team>.app.coralogix.com` | `coralogix.com` |
| `https://<team>.app.eu2.coralogix.com` | `eu2.coralogix.com` |
| `https://<team>.app.coralogix.us` | `coralogix.us` |
| `https://<team>.app.us2.coralogix.com` | `us2.coralogix.com` |
| `https://<team>.app.coralogix.in` | `coralogix.in` |
| `https://<team>.app.ap2.coralogix.com` | `ap2.coralogix.com` |
| `https://<team>.app.ap3.coralogix.com` | `ap3.coralogix.com` |

Do not prefix the domain with `ingress.`. The script adds that.

Keep the application and subsystem names stable. Renaming them starts a new history in Coralogix.

| Variable | Recommended value |
|----------|-------------------|
| `CX_APPLICATION_NAME` | `bigfix` |
| `CX_SUBSYSTEM_NAME` | `inventory` |

### Step 4 — Configure environment

Secrets stay on the host. They are gitignored.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

| Variable | Example / notes |
|----------|-----------------|
| `BIGFIX_URL` | `https://bigfix.example.com:52311` |
| `BIGFIX_USERNAME` | Console operator from Step 2 |
| `BIGFIX_PASSWORD` | That operator's password |
| `CORALOGIX_PRIVATE_KEY` | Send Your Data key from Step 3 |
| `CORALOGIX_DOMAIN` | Domain from the table above |
| `CX_APPLICATION_NAME` | `bigfix` |
| `CX_SUBSYSTEM_NAME` | `inventory` |

Load it into the shell for the manual runs in the next step:

```bash
set -a && source .env && set +a
```

### Step 5 — Dry run, then ship once

Dry run queries BigFix and prints one sample log. It does not call Coralogix. The Send Your Data key is not required for this command.

```bash
source .venv/bin/activate
python bigfix_inventory_to_coralogix.py --dry-run
```

Check the printed record. You want a real computer name, an operating system, and a last report time. The line `Dry run: would ship N log(s)` should match the number of computers the operator can see.

To exercise the path with a handful of computers first:

```bash
python bigfix_inventory_to_coralogix.py --dry-run --max-computers 5
```

When the sample looks right, ship the full snapshot:

```bash
python bigfix_inventory_to_coralogix.py
```

A successful run ends with `Done. Shipped N computer inventory log(s)`.

### Step 6 — Schedule it once a day

Edit `crontab.example` and replace `/opt/bigfix-inventory-coralogix` with the directory you cloned into. Then:

```bash
mkdir -p logs
crontab crontab.example
crontab -l
```

Or add this line yourself with `crontab -e` (change the path):

```cron
SHELL=/bin/bash
15 2 * * * /opt/bigfix-inventory-coralogix/scripts/run_daily.sh >> /opt/bigfix-inventory-coralogix/logs/inventory.log 2>&1
```

`scripts/run_daily.sh` loads `.env` and runs the shipper. Cron does not inherit the variables from your interactive shell.

The next morning:

```bash
tail -n 50 logs/inventory.log
```

The log should show one run, the computer count, and `Done. Shipped ...`.

### Step 7 — Validate in Coralogix

In **Logs**, set the time range to **Last 24 hours** and run:

```text
source logs
| filter $l.applicationname == 'bigfix'
| filter $l.subsystemname == 'inventory'
| aggregate count() as computers
```

The count should be close to the computer count printed by the script. Open one event and confirm `event_type` is `computer_inventory` and `snapshot_id` is the time of that run.

A second run the same day adds another full snapshot. It does not update the previous one. That is why the schedule is once a day.

---

## What each log contains

`full` (the default) sends:

| Field | BigFix source |
|-------|----------------|
| `computer_id` | Computer id |
| `computer_name` | Computer name |
| `dns_name` | DNS name |
| `operating_system` | OS |
| `ip_addresses` | IP addresses, joined with `;` |
| `last_report_time` | Last report time |
| `agent_version` | Agent version |
| `agent_type` | Agent type |
| `device_type` | Device type |
| `license_type` | License type |
| `active_directory_path` | Active Directory path |
| `cpu` | CPU |
| `relay_server` | Relay |
| `relay_hostname` | Relay hostname |
| `locked` | Locked flag |
| `root_server` | BES root server |

Every event also has `source` = `bigfix`, `event_type` = `computer_inventory`, `snapshot_id`, and `stale` (`true` when the last report is older than `BIGFIX_STALE_DAYS` or missing).

`basic` sends only the first eight fields. Use it when a newer inspector is not available on your server:

```env
BIGFIX_PROFILE=basic
```

To export one computer group only:

```env
BIGFIX_COMPUTER_GROUP=Windows Workstations
```

The group name must match a single BigFix computer group.

---

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BIGFIX_PROFILE` | `full` | `full` or `basic` |
| `BIGFIX_COMPUTER_GROUP` | empty | Export members of this computer group |
| `BIGFIX_RELEVANCE` | built in | Custom Session Relevance. Requires `BIGFIX_COLUMNS`. Ignores `BIGFIX_COMPUTER_GROUP`. |
| `BIGFIX_COLUMNS` | built in | Comma-separated names, one per result column |
| `BIGFIX_VERIFY_SSL` | `true` | Set `false` only when the root server uses a private CA and you do not have the PEM |
| `BIGFIX_CA_BUNDLE` | empty | Path to a PEM bundle. Overrides `BIGFIX_VERIFY_SSL`. |
| `BIGFIX_TIMEOUT_SECONDS` | `600` | HTTP timeout for the BigFix query and for each Coralogix batch |
| `BIGFIX_STALE_DAYS` | `7` | Last report older than this is severity Warning |
| `CORALOGIX_BATCH_SIZE` | `50` | Logs per Coralogix POST |
| `DEBUG` | `false` | Print the resolved settings. Secrets are redacted. |

---

## Security

- `.env` is gitignored. Do not force-add it.
- `chmod 600 .env`
- Use a dedicated BigFix operator and a dedicated Send Your Data key for this job.
- Rotate both credentials on your normal schedule.
- Prefer `BIGFIX_CA_BUNDLE` over `BIGFIX_VERIFY_SSL=false`.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `Missing required environment variable` | `.env` was not loaded | `set -a && source .env && set +a`, or run `scripts/run_daily.sh` |
| BigFix `401` | Wrong operator password, or the account cannot use the REST API | Repeat the `curl` check in Step 2 |
| BigFix `403` | Operator cannot see those computers | Widen the operator's computer assignments, or use a Master Operator |
| `The operator "…" is not defined` | That inspector is not on this BigFix version | Set `BIGFIX_PROFILE=basic` |
| `certificate verify failed` | Private CA on the root server | Set `BIGFIX_CA_BUNDLE` to the PEM, or `BIGFIX_VERIFY_SSL=false` |
| Query times out | Large deployment or a slow Web Reports server | Raise `BIGFIX_TIMEOUT_SECONDS`. The REST API forwards the query to BigFix Explorer or Web Reports. |
| Coralogix `401` or `403` | Wrong key type or wrong domain | Use a **Send Your Data** key. Check `CORALOGIX_DOMAIN` against Step 3. |
| Logs are missing in **Last 24 hours** | Cron did not run, or the labels changed | Read `logs/inventory.log`. Filter on `bigfix` / `inventory`. |
| Count is lower than the console | Operator is not a Master Operator, or `BIGFIX_COMPUTER_GROUP` is set | Expected. The API returns only computers that operator can administer. |
| `self-test ok` but dry run fails | Network or credentials, not the parser | The self-test does not contact BigFix |

---

## Manual run

```bash
cd /opt/bigfix-inventory-coralogix
source .venv/bin/activate
set -a && source .env && set +a
python bigfix_inventory_to_coralogix.py --dry-run
python bigfix_inventory_to_coralogix.py
```
