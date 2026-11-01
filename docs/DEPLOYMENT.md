# Deployment

photosort is one container. It needs two things mounted: a small `data/` folder for its
settings and history, and the folder that holds your photos.

## With the repository's compose file

```bash
git clone https://github.com/PhilippMundhenk/photosort.git && cd photosort
cp .env.example .env          # PUID/PGID, TZ, PHOTOS_BASE: see below
docker compose pull && docker compose up -d
# or build the image yourself:  docker compose up -d --build
```

`.env`:

| Variable | What |
|---|---|
| `PUID`, `PGID` | the user the container runs as: the owner of the photos on the share |
| `TZ` | your home time zone (`Europe/Berlin`); later editable in Settings |
| `PHOTOS_BASE` | the host folder holding `inbox/` and `sorted/`, mounted at `/photos` |
| `PHOTOSORT_IMAGE` | another image than `ghcr.io/philippmundhenk/photosort:latest` |

Open `http://<host>:8080`. The first scan reads every photo once with exiftool (a few minutes
for 20 000 files over a share); afterwards only new files are read. The compose file limits the
container to 1 GB of memory, enough for a library of 20 000 photos.

Updating: `docker compose pull && docker compose up -d`. Settings, proposals and history stay
in `data/`.

## The photo folders

Mount **one** folder that holds both `inbox/` and `sorted/`, at `/photos`:

```
/mnt/nas/photos/            → /photos
├── inbox/                  → /photos/inbox   (Settings: inbox folder)
│   ├── phone/              an input, named after the folder
│   ├── partner/
│   └── camera/
└── sorted/                 → /photos/sorted  (Settings: sorted folder)
```

A move is then a rename on the share, instant even for videos. Two separate mounts make every
move a copy through the container and back over the network; the pages warn when they see
that. Every subfolder of the inbox is an input, found again on every scan, so a new device
folder needs no setup. Each input is read recursively. Other layouts: set the folders, or list
the inputs by hand (`name=/path`), in Settings.

Keep each device in its own folder: photosort follows every device on its own timeline, and
per-device settings (a camera's clock, its GPS) go by folder name.

## NAS share mounts

The share must be mounted on the host before the container starts: Docker binds what is at
`PHOTOS_BASE` at that moment. A systemd `.automount` does not work, because Docker binds the
empty placeholder and the share mounted later never shows up inside the container. Mount the
share at boot with a `.mount` unit instead; `nofail` lets the host boot without the NAS,
`_netdev` waits for the network. For a CIFS share at `/mnt/nas/photos` (the file name must
match the path: `/etc/systemd/system/mnt-nas-photos.mount`):

```ini
[Unit]
Description=NAS photos share
After=network-online.target
Wants=network-online.target

[Mount]
What=//nas.local/photos
Where=/mnt/nas/photos
Type=cifs
Options=credentials=/etc/nas-credentials,uid=1000,gid=1000,file_mode=0664,dir_mode=0775,nofail,_netdev

[Install]
WantedBy=multi-user.target
```

For NFS use `What=nas.local:/volume1/photos`, `Type=nfs` and `Options=nofail,_netdev`. Then
`sudo systemctl daemon-reload && sudo systemctl enable --now mnt-nas-photos.mount`.

To keep an automount, use the long volume syntax with `propagation: rslave` and touch the path
before starting, so the share is mounted when Docker binds it:

```yaml
    volumes:
      - type: bind
        source: ${PHOTOS_BASE:-/mnt/nas/photos}
        target: /photos
        bind:
          propagation: rslave
```

```bash
ls /mnt/nas/photos > /dev/null && docker compose up -d
```

## Permissions

With CIFS, `uid=` and `gid=` of the mount must be the same numbers as `PUID` and `PGID`: the
container runs as that user. If the share belongs to someone else, reading works but every
move fails, because the folders are `0755` and "other" may read but not create or rename. With
NFS the owners come from the NAS; there `PUID`/`PGID` must be the NAS user that owns the photos.

An inbox you only want to read from (mounted read-only, or another tool's folder): switch on
*Copy instead of move* in Settings. The sorted folder must be writable either way. See
[USAGE.md](USAGE.md#copy-mode-and-read-only-inboxes).

## Behind a reverse proxy

photosort has no login of its own: put it behind your proxy's authentication if it is reachable
from outside your network. The compose file has commented Traefik labels (with an Authelia
middleware) to adapt. Set *Address of photosort* in Settings when Matrix messages should link to
the proxy's address.

## Commands in the container

The container is called `photosort`. `docker exec photosort …` works from any folder;
`docker compose exec photosort …` only in the folder with `docker-compose.yml`.

```bash
docker exec photosort id                 # which user the container runs as
docker exec photosort ls -l /photos      # is the share there, and who owns it?
docker logs -f photosort                 # follow a scan
```

## Thumbnails

On a Synology, photosort uses the thumbnails the NAS made (`@eaDir`, pattern in Settings)
and decodes nothing itself. Elsewhere it makes its own once, in a background helper process,
into `data/thumbs/` (RAW previews via exiftool, one frame per video via ffmpeg). Switch that off
in Settings on a very small box: photos without a NAS thumbnail then show a placeholder.
