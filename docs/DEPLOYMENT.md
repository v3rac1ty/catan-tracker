# Deployment guide

This guide deploys Catan Tracker on an Ubuntu VM with Docker Compose. It uses an
Oracle Cloud Infrastructure (OCI) Ampere A1 VM as the concrete example. The
Compose workflow is also suitable for another always-on Linux host.

The bot accepts no inbound web traffic. It connects outbound to Discord, and
PostgreSQL is published only on the host loopback interface. The deployment
therefore needs no public bot or database port.

## 1. Create and install the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, and add a bot user.
2. On **Bot**, reset and copy the token. Treat the token like a password. Do not
   paste it into chat, commit it, or put it in a command line.
3. Leave privileged intents disabled. Catan Tracker does not require Message
   Content, Server Members, or Presence intent.
4. On **Installation**, enable **Guild Install**. Configure the `bot` and
   `applications.commands` scopes and grant these bot permissions:

   - View Channels
   - Send Messages
   - Embed Links

5. Use the generated install link to add the bot to the intended server. A
   server administrator must approve the install.
6. To get the server ID for a fast, server-only command sync, enable Developer
   Mode in Discord, right-click the server, and select **Copy Server ID**.

Discord documents the [application installation
flow](https://docs.discord.com/developers/resources/application#installation),
[OAuth2 scopes](https://docs.discord.com/developers/topics/oauth2), and
[bot-token handling](https://docs.discord.com/developers/bots/overview).

If the configured announcement channel has channel-specific permission
overrides, make sure the bot can view that channel, send messages, and embed
links there as well.

## 2. Create an OCI Ampere VM

Always Free resources must be created in the account's home region. Current
limits and eligibility can change, so check Oracle's [Always Free resource
page](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
before provisioning.

1. In the OCI Console, open **Compute > Instances > Create instance**.
2. Select an Always Free-eligible Ubuntu image and the
   `VM.Standard.A1.Flex` Ampere shape. One OCPU and 6 GB of RAM is a reasonable
   starting size for this bot and its PostgreSQL container and stays within the
   documented A1 allowance at the time of writing.
3. Add your SSH public key. Keep the private key only on trusted admin
   machines and set it to owner-read/write only with `chmod 600`.
4. Place the instance in a VCN subnet with outbound internet access. Assign a
   public IP only if you will administer the host directly over SSH; OCI
   Bastion is an alternative for a private instance.
5. In a network security group, allow stateful inbound TCP port 22 only from
   the administrator's fixed public IP or trusted CIDR. Do not add inbound
   rules for PostgreSQL port 5432 or an application HTTP port. Leave outbound
   access available for DNS, time synchronization, OS packages, container
   images, and Discord HTTPS/WebSocket connections.

OCI recommends network security groups for VNIC-specific policy and documents
how [security rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm)
and [instance networking](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)
work. The guest OS firewall and OCI network rules both apply.

Connect as the image's Ubuntu user:

```bash
ssh -i ~/.ssh/oci_catan ubuntu@VM_PUBLIC_IP
```

Then update the host and install the small set of local tools used below:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl git openssl
```

Free capacity can be unavailable in an availability domain, and Oracle may
reclaim idle Always Free compute under its current policy. Treat Always Free
availability as best-effort, keep tested backups off the VM, and monitor the
current account limits.

## 3. Install Docker Engine and Compose

These commands follow Docker's current [Ubuntu installation
instructions](https://docs.docker.com/engine/install/ubuntu/) and install the
Compose plugin from Docker's official repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

This guide keeps Docker administration behind `sudo`. Membership in the
`docker` group grants root-equivalent control over the host; Docker explains
that tradeoff in its [Linux post-install
guide](https://docs.docker.com/engine/install/linux-postinstall/).

On an Ampere VM, `dpkg --print-architecture` should report `arm64`. The
repository builds from the official `python:3.12-slim` image and runs the
official `postgres:16-alpine` image. Both are published for ARM64, so no local
Dockerfile changes are required. The same Compose file also works on AMD64.

## 4. Clone and configure

Install the repository under `/opt`:

```bash
sudo install -d -o "$USER" -g "$USER" /opt/catan-tracker
git clone https://github.com/v3rac1ty/catan-tracker.git /opt/catan-tracker
cd /opt/catan-tracker
cp .env.example .env
chmod 600 .env
```

Generate a separate value for each database password:

```bash
openssl rand -hex 24
openssl rand -hex 24
openssl rand -hex 24
```

Edit `.env` and set:

- `POSTGRES_PASSWORD`, `MIGRATOR_PASSWORD`, and `APP_PASSWORD` to the three
  distinct generated values. `MIGRATOR_PASSWORD` and `APP_PASSWORD` must each
  contain at least 16 characters and use only ASCII letters, digits,
  underscores, or hyphens because Compose embeds them in PostgreSQL DSNs. The
  recommended hex generator satisfies this policy.
- `DISCORD_TOKEN` to the token copied from the Developer Portal.
- `DEV_GUILD_ID` to the server ID for the first sync, or leave it empty for a
  global sync.
- `SYNC_COMMANDS=true` for the first successful startup of this command
  version.
- `DB_HOST_PORT` to an unused local port. Its default is 5432.

The four connection strings near the bottom of `.env.example` are for commands
run directly on the host. Compose constructs its own internal connection
strings, so those host-only values can retain their placeholders on a
production Compose host.

Validate the resolved Compose model without printing secrets:

```bash
sudo docker compose config --quiet
stat -c '%a %n' .env
```

The permission check should report `600 .env`. Do not use plain `docker
compose config` in shared logs because its output includes resolved environment
values. Docker users with access to the daemon can also inspect container
environment variables, so limit SSH and Docker access to trusted operators.

The database is bound as `127.0.0.1:DB_HOST_PORT`, while the bot and migration
containers reach it over the private Compose network. Keep that loopback
binding. Docker warns that published container ports can bypass some host
firewall rules; the loopback binding plus the OCI ingress policy prevents
public database access.

## 5. First startup and command sync

Build the image, initialize PostgreSQL, apply migrations, start the bot, and
perform the one-time command sync:

```bash
cd /opt/catan-tracker
sudo docker compose up -d --build
sudo docker compose ps --all
sudo docker compose logs --tail=100 migrate
sudo docker compose logs --tail=100 bot
```

Expected state:

- `db` is running and healthy.
- `migrate` exited with code 0. It is intentionally a one-shot service.
- `bot` is running, connected, and reports a successful command sync.

The one-shot migration service applies every pending migration, including
`0004_game_updates.sql` (confirmed-game correction and audit revision
storage) and `0005_score_collection.sql` (per-player DM score collection
and the recurring leaderboard post). Confirm it completed successfully
before using `/game update`, `/game report`, or `/config leaderboard`.

A sync with `DEV_GUILD_ID` is limited to that server and should appear quickly.
A global sync can take longer to propagate. After the sync succeeds, change
`SYNC_COMMANDS=false` and recreate only the bot container:

```bash
sudo docker compose up -d --no-deps --force-recreate bot
```

Run another one-time sync after installing a release that changes slash-command
definitions, including a release that adds or changes `/game update` or
`/config leaderboard`. Avoid syncing on every restart because it makes
unnecessary Discord API calls.

In Discord, use `/config channel` to select the season-announcement channel,
then set the server timezone and optional admin role. To enable event
notifications, use `/config player-role` with a role that exists in this
server. The role should be mentionable. If it is intentionally not
mentionable, grant the bot `Mention @everyone, @here, and All Roles` in every
channel where events may be posted; Discord permits the bot to mention that
role only with that channel permission. The bot also needs View Channel, Send
Messages (or Send Messages in Threads for a thread), and Embed Links in the
destination. If the role is deleted, becomes unmentionable, or the permission
is removed, the scheduler logs the condition and delivers an unpinged
reminder. Clear the setting with `/config player-role` without a role to keep
notifications silent.

Event reminders go to the channel where `/event create` was run. Both
`/event create` and `/leaderboard` accept an optional destination channel and
show an ephemeral confirmation when one is supplied. Use `/config show` to
review the saved configuration.

## 6. Health checks and logs

Use these checks during routine operation:

```bash
sudo docker compose ps --all
sudo docker compose exec -T db pg_isready -U postgres -d catan
sudo docker compose logs --tail=100 bot
sudo docker compose logs --tail=100 db
```

Follow bot logs during troubleshooting, then exit with `Ctrl-C`:

```bash
sudo docker compose logs --tail=100 --follow bot
```

The Compose file limits each container's JSON log files to three 10 MB files.
Logs intentionally avoid database exception details and user-provided text, but
still restrict host and Docker access because operational metadata can be
sensitive.

## 7. Safe updates

Back up the database before any update that may apply a migration. Then update
with a fast-forward-only pull:

```bash
cd /opt/catan-tracker
git status --short
PREVIOUS_REVISION=$(git rev-parse HEAD)
git fetch --prune
git pull --ff-only
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps --all
sudo docker compose logs --tail=100 migrate bot
```

Investigate a nonempty `git status --short` before pulling; production-only
changes should live in `.env`, which Git ignores. Save `PREVIOUS_REVISION` in
the maintenance record together with the backup filename.

To roll back application code, check out a known release tag or the recorded
revision and rebuild:

```bash
git switch --detach "$PREVIOUS_REVISION"
sudo docker compose up -d --build
```

A code rollback does not reverse a database migration. If the prior release is
incompatible with the migrated schema, restore the backup made immediately
before the update, then start the prior code. Test this combination before an
urgent rollback. Return the checkout to the intended release branch or tag at
the next maintenance window.

## 8. Backups and restore tests

The named Docker volume persists through container recreation, but it is not a
backup. Create a compressed PostgreSQL archive with restrictive permissions:

```bash
cd /opt/catan-tracker
install -d -m 700 backups
umask 077
BACKUP="backups/catan-$(date +%F-%H%M).dump"
sudo docker compose exec -T db \
  pg_dump -U catan_migrator -d catan -Fc > "$BACKUP"
test -s "$BACKUP"
sudo docker compose exec -T db pg_restore -l < "$BACKUP" >/dev/null
echo "$BACKUP"
```

The repository ignores `backups/` so archives do not make the production
checkout dirty or enter commits accidentally. Git exclusion is not an access
control: keep the directory at mode 700 and its files at mode 600. Copy each
archive to encrypted storage outside the VM because a backup stored only on the
instance is lost with the boot volume or account.

### Rehearse a restore without touching production

Use a disposable database to prove that the archive can be restored. These
commands delete only the database named `catan_restore_test`:

```bash
RESTORE_DB=catan_restore_test
sudo docker compose exec -T db \
  dropdb -U postgres --if-exists "$RESTORE_DB"
sudo docker compose exec -T db \
  createdb -U postgres -O catan_migrator "$RESTORE_DB"
sudo docker compose exec -T db \
  pg_restore -U catan_migrator --exit-on-error --no-owner --no-privileges \
  -d "$RESTORE_DB" < "$BACKUP"
sudo docker compose exec -T db \
  psql -U catan_migrator -d "$RESTORE_DB" -v ON_ERROR_STOP=1 \
  -c 'SELECT COUNT(*) AS restored_seasons FROM seasons;'
sudo docker compose exec -T db \
  dropdb -U postgres --if-exists "$RESTORE_DB"
```

Schedule and record a restore rehearsal regularly. A successful `pg_dump`
alone does not prove that an archive is usable.

### Restore production

Restoring replaces current data. First take a separate safety backup, stop the
bot, and confirm that the chosen archive and application revision belong
together. Then:

```bash
sudo docker compose stop bot
sudo docker compose exec -T db \
  pg_restore -U catan_migrator --exit-on-error --clean --if-exists \
  --no-owner --no-privileges -d catan < "$BACKUP"
sudo docker compose run --rm migrate
sudo docker compose up -d bot
sudo docker compose ps --all
sudo docker compose logs --tail=100 migrate bot
```

If any restore step fails, keep the bot stopped, preserve the archive and logs,
and restore the separate safety backup before resuming service.

### Automate backups

Choose cron or a systemd timer. Both examples run as root so they can access
the Docker daemon. Adjust `/opt/catan-tracker` if the checkout lives elsewhere.

For cron, run `sudo crontab -e` and add:

```cron
17 3 * * * cd /opt/catan-tracker && umask 077 && mkdir -p backups && /usr/bin/docker compose exec -T db pg_dump -U catan_migrator -d catan -Fc > "backups/catan-$(date +\%F-\%H\%M).dump" 2>> backups/backup.log
27 4 * * * find /opt/catan-tracker/backups -type f -name 'catan-*.dump' -mtime +14 -delete
```

For systemd, create `/etc/systemd/system/catan-backup.service`:

```ini
[Unit]
Description=Back up the Catan Tracker database
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/catan-tracker
UMask=0077
ExecStart=/bin/sh -c 'mkdir -p backups && /usr/bin/docker compose exec -T db pg_dump -U catan_migrator -d catan -Fc > "backups/catan-$(date +%%F-%%H%%M).dump"'
ExecStart=/usr/bin/find /opt/catan-tracker/backups -type f -name catan-*.dump -mtime +14 -delete
```

Create `/etc/systemd/system/catan-backup.timer`:

```ini
[Unit]
Description=Run the Catan Tracker database backup daily

[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer and inspect its first manual run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now catan-backup.timer
sudo systemctl start catan-backup.service
sudo systemctl status catan-backup.service
sudo systemctl list-timers catan-backup.timer
```

Automation should also copy verified archives off-host and alert on failures.
The examples provide local rotation only.

## Secret and access checklist

- Keep `.env`, SSH private keys, and backup archives out of Git and readable
  only by their owner. The recommended modes are 600 for files and 700 for
  backup directories.
- Limit OCI SSH ingress to trusted administrator addresses. Remove access for
  former operators promptly.
- Never expose port 5432 publicly or change the Compose binding from
  `127.0.0.1` to all interfaces.
- Rotate a compromised Discord token in the Developer Portal, update `.env`,
  and recreate the bot container.
- Database role passwords are set when the PostgreSQL volume is first
  initialized. Changing only `.env` later does not change existing roles and
  will break authentication. Rotate them as a coordinated database operation
  with an administrator, then recreate the affected containers.
- Keep Ubuntu and Docker security updates current, and retest startup plus a
  restore after material upgrades.
