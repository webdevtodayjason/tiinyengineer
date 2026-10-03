# TiinyEngineer

TiinyEngineer watches the quiet failures that are easy to miss: a web page that stops answering, a container that never comes back, a backup that goes stale, or a scheduled job that silently stops running. It runs beside your Tiiny on a computer or small box that stays on, keeps its history in a local SQLite database, opens one incident per problem, and closes that same incident when the check recovers.

## What you need

- A Tiiny on the same local network.
- A computer or small box that stays on and can run Python 3.11.
- Network access to the services you choose to watch.

TiinyEngineer uses only the Python standard library. Alert services and infrastructure sources are optional.

## Install from the farm

Open the Tiiny app farm, choose TiinyEngineer, and select **Plant it**. The launcher installs the app, creates its data folder, starts it, and opens the dashboard. No terminal setup is required.

On the first run, TiinyEngineer discovers Tiiny devices through their documented `device.json` discovery response and adds the farm health page. The rest of the Registry starts empty, with an invitation to add your first check. Existing databases are never seeded again, so upgrades do not replace or add checks.

## Dashboard

- **Now** shows open incidents and the current state of every check.
- **Registry** adds a web address, host, service, device, or scheduled job.
- **Rules** controls timing, grace, severity, maintenance windows, and the first verification step.
- **Reporting** shows which configured signal paths receive a real incident.
- **Assignment** shows the owner for each check.
- **Timeline** is the append-only incident ledger.
- **History** shows the last 36 hours at a glance.
- **Settings** configures source connections, alerts, and write-only secrets.

## Alerts

Alerts are optional. Settings can connect an Alerts view endpoint, Telegram, and a Watchtower-style off-site heartbeat. Test messages use one channel only and begin with `TEST`.

Every outage alert uses five plain-language lines:

1. State and what is affected.
2. Since when and how sure the watcher is.
3. What the failure likely means.
4. What to do first and who owns it.
5. Useful links, such as the Alerts view row or a service console.

A recovery begins with `UP again after N min`. The recovery closes the original incident instead of creating a second one.

## Settings and data

Every runtime setting is available on the Settings screen. Secret values are write-only and stored with restricted permissions. The database, ledger export, and source settings live under `FARM_DATA_DIR`, so app upgrades can replace the code without replacing your data.

## Uninstall

Use the farm launcher to remove TiinyEngineer. Choose whether to keep its data when prompted. Keeping the `FARM_DATA_DIR` folder preserves the Registry and history for a later reinstall; removing that folder permanently removes the local database and saved settings.

## License

TiinyEngineer is released under the MIT License. See [LICENSE](LICENSE).
