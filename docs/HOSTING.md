# Hosting on a personal server with NGINX

The hosted application is the generated HTML file. It needs no Python web process, MongoDB connection, Node.js, WebSocket, or API on the web server. You can generate it on another machine, upload it, and publish it as `index.html`.

These instructions target Ubuntu 22.04+ or Debian 12+ with systemd and the distribution's NGINX package. They add two named configuration files under `/etc/nginx/conf.d/`; they do not replace `nginx.conf` or your existing virtual hosts. Choose an unused origin port and a unique domain. Other distributions may use different package names, service names, or NGINX worker accounts.

```text
Browser -> HTTPS NGINX proxy + password -> 127.0.0.1:8088 NGINX static origin
                                              |
                                 /var/lib/emoticon-lookup/www/index.html
```

Both NGINX server blocks run in the same NGINX service. The loopback origin is useful when the personal server already routes applications through an NGINX reverse proxy. Use the existing-site route below if HTTPS is already configured.

## 1. Install server prerequisites

Run these commands on the server as a normal SSH user with sudo access:

```bash
sudo apt-get update
sudo apt-get install nginx apache2-utils python3 python3-venv git curl
git clone https://github.com/rndmzd/emoticon-lookup.git
cd emoticon-lookup
sudo install -d -m 0755 /var/lib/emoticon-lookup /var/lib/emoticon-lookup/www /var/lib/emoticon-lookup/acme
```

For generation **on this server**, run `bash scripts/setup.sh` as your normal user. It checks Python 3.10+, creates `.venv`, installs generator dependencies, and checks their compatibility. It is unnecessary if you upload a gallery generated elsewhere. Do not run setup as root or use a Windows virtual environment on Linux.

## 2. Generate or upload the gallery

To generate here, keep the connection string in the current shell environment:

```bash
read -rsp 'MongoDB connection URI: ' MONGODB_URI; printf '\n'
export MONGODB_URI
.venv/bin/python emoticon_lookup.py --user YOUR_USERNAME --output generated/emoticons.html
unset MONGODB_URI
```

`localhost` in the MongoDB URI refers to the machine running the generator. If MongoDB is on your workstation, generate there or use a reachable, authorized database address. The NGINX server never needs the URI.

To generate on Windows, follow the README, then upload **only the HTML**:

```powershell
scp .\generated\emoticons.html serveruser@your-server:~/emoticons.html
```

Publish on the server:

```bash
# For server-generated output:
sudo python3 scripts/publish_gallery.py generated/emoticons.html

# OR for an uploaded file:
sudo python3 scripts/publish_gallery.py ~/emoticons.html
```

For a first test without MongoDB or real user history, publish `examples/animation_demo.html` instead. The publisher copies just one complete HTML document, checks that its source stays unchanged during copying, and atomically replaces `/var/lib/emoticon-lookup/www/index.html`. It keeps the previous version in `/var/lib/emoticon-lookup/state/previous.html`, outside the served directory. Permissions are 0644 for the served file and 0600 for the backup. It does not copy message exports, manifests, caches, or connection files.

## 3A. Use your existing HTTPS NGINX site

Use this route if your domain and TLS certificate already work. Generate only the loopback origin in a separate output directory:

```bash
python3 deploy/render_nginx.py --domain your-existing-domain.example --origin-only --output-dir generated/nginx-origin
sudo bash scripts/install_nginx.sh generated/nginx-origin
curl -f http://127.0.0.1:8088/healthz
curl -I http://127.0.0.1:8088/
```

Create the password file, prompting for a password rather than putting it in an argument:

```bash
# First user only: -c creates/replaces the password file.
sudo htpasswd -c /etc/nginx/emoticon-lookup.htpasswd galleryreader
sudo chown root:www-data /etc/nginx/emoticon-lookup.htpasswd
sudo chmod 0640 /etc/nginx/emoticon-lookup.htpasswd
```

If this file already exists, omit `-c` to add/change a user. `www-data` is the usual Debian/Ubuntu NGINX worker group; use the configured worker group if yours differs.

Paste [deploy/nginx/location.conf.example](../deploy/nginx/location.conf.example) **inside your existing HTTPS `server` block**. It serves the gallery at `/emoticons/`. Its trailing `proxy_pass` slash strips that prefix before forwarding requests to the origin. Change its upstream port if you chose a different origin port.

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Open `https://YOUR_EXISTING_DOMAIN/emoticons/`. Continue at verification below; skip the dedicated-domain instructions.

## 3B. Use a dedicated domain and a new HTTPS frontend

Point a DNS A record, and an AAAA record only if IPv6 is actually routed to this server, at the server's public address. Forward/allow TCP 80 and 443 to the NGINX host if needed. Check the current firewall and retain SSH access; these scripts do not modify firewall rules or router settings. The origin port stays bound to loopback and is not forwarded.

Render and install the HTTP certificate-bootstrap configuration:

```bash
python3 deploy/render_nginx.py --domain emoticons.example.com
sudo bash scripts/install_nginx.sh generated/nginx
curl -f http://127.0.0.1:8088/healthz
```

Replace `emoticons.example.com` with your real hostname. Before TLS setup, the domain serves only ACME challenge files; the gallery URL deliberately returns 404. The rendered files are in the Git-ignored `generated/nginx/` directory. The installer preserves prior versions of its two named snippets, runs `nginx -t` on the full configuration, and reloads or starts the NGINX service. If installation or validation fails, it restores those snippets and prints the backup directory. It does not edit other sites. Check the displayed errors before retrying.

Get a certificate using Certbot's webroot mode:

```bash
sudo apt-get install certbot
sudo certbot certonly --webroot -w /var/lib/emoticon-lookup/acme -d emoticons.example.com
```

Complete Certbot's interactive prompts. HTTP-01 validation requires the domain's port 80 to be reachable from the internet. For LAN-only hosts or connections without inbound port 80, use your existing TLS proxy or obtain a certificate using your DNS provider's supported DNS challenge workflow.

Create the password file as shown in route 3A. Then render HTTPS and install it:

```bash
python3 deploy/render_nginx.py --domain emoticons.example.com --https
sudo bash scripts/install_nginx.sh generated/nginx
```

HTTPS uses the certificate under `/etc/letsencrypt/live/DOMAIN/`. The HTTP frontend now redirects to HTTPS while retaining the ACME challenge location. HTTPS requires a password and proxies to `127.0.0.1:8088`. `proxy_buffering off` avoids spooling large embedded galleries into NGINX proxy temporary files; a 300-second upstream read timeout allows slower transfers. These are download responses, so raising `client_max_body_size` is unnecessary.

For existing certificates or custom paths, use `--certificate`, `--certificate-key`, `--password-file`, `--web-root`, `--acme-root`, and `--origin-port`. Paths must be absolute Linux paths using letters, numbers, `/`, `.`, `_`, or `-`. Create custom directories with permissions allowing the NGINX worker to traverse and read them; the installer prepares only the default directories. Use the same custom web root with `publish_gallery.py --web-root /your/path`.

## 4. Verify the hosted site

For a dedicated domain:

```bash
curl -I https://emoticons.example.com/                  # 401 without a password
curl -I -u galleryreader https://emoticons.example.com/ # prompts for password; expect 200
curl -I -u galleryreader https://emoticons.example.com/emoticons.json # 404 after authentication
```

For the existing-site route, use `https://YOUR_DOMAIN/emoticons/` and `https://YOUR_DOMAIN/emoticons/emoticons.json` instead.

Open the HTTPS URL in a browser. Confirm images animate, clicking an image opens the full-size overlay, copy-name works, groups can be created, and a saved portable copy reopens with its groups. HTTPS supports the browser clipboard API. Group changes are browser-local; there is no server-side upload or group-write endpoint. To share edited groups, choose **Save portable copy**, upload that HTML, and republish it.

The origin serves only `/`, `/index.html`, and `/healthz`. It does not serve arbitrary files or directories. Generated galleries contain usernames and usage counts, so the supplied frontend uses password protection. If you already use an authentication gateway, integrate the location with that gateway according to your existing configuration.

## 5. Updates, rollback, and certificate renewal

Generate a new HTML file or save an edited portable copy, upload it, and rerun the publisher. No NGINX reload is needed to update the HTML. An in-progress download can finish reading the old file while new requests receive the replacement. Published pages send `Cache-Control: private, no-store`; reload an already-open tab to load a new version. Run one publisher at a time.

```bash
sudo python3 scripts/publish_gallery.py ~/new-emoticons.html
sudo python3 scripts/publish_gallery.py --rollback
```

Rollback swaps back to the one saved previous version; the version it replaces becomes the new rollback candidate. You can specify `--state-dir` to relocate backups, but it must remain outside the web root. Keep your original generated files if you need a longer history.

Pull project updates as the checkout's normal user and refresh generator dependencies if you generate on the server:

```bash
git pull --ff-only
bash scripts/setup.sh
```

Changing scripts does not update an already-generated gallery's interface. Regenerate and publish the gallery to pick up new UI features. Re-render/reinstall NGINX snippets only when configuration changes; preserve your chosen domain, origin port, paths, and authentication settings.

For Certbot webroot certificates, create a deploy hook so NGINX reloads after renewal:

```bash
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
sudo install -m 0755 deploy/certbot-reload.sh /etc/letsencrypt/renewal-hooks/deploy/emoticon-lookup-reload
sudo certbot renew --dry-run
systemctl list-timers --all | grep certbot
```

Confirm your Certbot installation schedules renewal; if no timer/job exists, configure scheduling using that installation's documented method. The hook runs `nginx -t` before reloading. The frontend's HTTP challenge location must remain reachable for webroot renewals.

## Troubleshooting and operational boundaries

- **502:** check NGINX service logs and the configured origin port; `curl -f http://127.0.0.1:8088/healthz` should succeed.
- **404 at the gallery root:** publish a file as `index.html` and confirm `--web-root` matches the origin configuration. The HTTP bootstrap root returns 404 intentionally.
- **403:** check directory traversal/file read permissions and the NGINX worker account. Do not serve the repository or a private home directory directly.
- **401 with a password:** verify the password file path, username, and worker read permissions. Use `htpasswd` without `-c` to update an existing file.
- **Certificate failures:** check DNS, port forwarding, IPv6 routing, and the ACME web root. `sudo nginx -t` reports missing certificate/key paths before activation.
- **A slow initial page load:** the single-file format includes every original animation. Large galleries can be hundreds of MB. Disabling preview animation reduces playback work but does not reduce the embedded download size. A CDN that enforces response-size limits may reject these files.

Use `sudo journalctl -u nginx -n 100 --no-pager` and the distribution's NGINX access/error logs. This repository supplies configuration and scripts; it does not automatically deploy to your server, configure DNS/firewall rules, obtain production certificates, or store shared group edits.

The same-host defaults are intentional. For a proxy on another machine, the origin must instead bind to a private interface, the proxy upstream must use that private address, and network rules must restrict access to the proxy. Keep credentials and MongoDB connectivity on the generation machine. That topology requires host-specific adjustments to the templates.

## References

- [NGINX proxy module: proxy_pass, buffering, headers, timeouts](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [NGINX HTTP Basic authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)
- [NGINX HTTPS configuration](https://nginx.org/en/docs/http/ngx_http_ssl_module.html)
- [Certbot webroot and renewal](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot)
