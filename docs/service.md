# Run the auto-capture as a service

`scripts/watch_and_capture.py` polls for the board and captures the strongest
band as soon as it appears. Keep it running as a systemd **user** service so it
survives logouts and starts at boot (needs `loginctl enable-linger $USER` once).

## Install

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/sdr7020-watch.service <<'EOF'
[Unit]
Description=PlutoSky 7020 auto survey and capture
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/Projects/sdr7020
ExecStart=/usr/bin/python3 %h/Projects/sdr7020/scripts/watch_and_capture.py
Restart=always
RestartSec=10
StandardOutput=append:%h/Projects/sdr7020/captures/watch.log
StandardError=append:%h/Projects/sdr7020/captures/watch.log

[Install]
WantedBy=default.target
EOF

loginctl enable-linger "$USER"          # once, so it starts without a login
systemctl --user daemon-reload
systemctl --user enable --now sdr7020-watch
```

## Check it

```bash
systemctl --user status sdr7020-watch
tail -f ~/Projects/sdr7020/captures/watch.log
```

## Before driving the board by hand

The watcher opens the USB gadget to run its survey, which makes `iio_info`,
`pyadi-iio` and `ptt.py` fail with `Device or resource busy`. Stop it first:

```bash
systemctl --user stop sdr7020-watch
# ... do your work ...
systemctl --user start sdr7020-watch
```

## Stop / remove

```bash
systemctl --user disable --now sdr7020-watch
rm ~/.config/systemd/user/sdr7020-watch.service
```

Captures land in `captures/auto_<timestamp>_<freq>MHz.*` (`.npy`, `.json`,
`.png` and `_survey.json`).
