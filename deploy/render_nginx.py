"""Render NGINX configuration files without installing or activating them."""
import argparse
from pathlib import Path
import re

TEMPLATES = Path(__file__).resolve().parent / "nginx"


def domain_name(value):
    value = value.lower().rstrip(".")
    if len(value) > 253 or "." not in value or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ValueError("Use a DNS hostname such as emoticons.example.com, without a scheme or path.")
    return value


def nginx_path(value):
    # Linux paths with no NGINX variable/directive interpolation.
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in value.split("/"):
        raise ValueError("Use an absolute Linux path containing letters, numbers, /, ., _, or -.")
    return value


def render(output, domain, port=8088, web_root="/var/lib/emoticon-lookup/www",
           acme_root="/var/lib/emoticon-lookup/acme", https=False,
           certificate=None, certificate_key=None,
           password_file="/etc/nginx/emoticon-lookup.htpasswd", origin_only=False):
    domain = domain_name(domain)
    if not 1024 <= port <= 65535 or port in (80, 443):
        raise ValueError("The loopback origin port must be between 1024 and 65535.")
    values = {
        "DOMAIN": domain, "ORIGIN_PORT": str(port), "WEB_ROOT": nginx_path(web_root),
        "ACME_ROOT": nginx_path(acme_root), "PASSWORD_FILE": nginx_path(password_file),
        "CERTIFICATE": nginx_path(certificate or f"/etc/letsencrypt/live/{domain}/fullchain.pem"),
        "CERTIFICATE_KEY": nginx_path(certificate_key or f"/etc/letsencrypt/live/{domain}/privkey.pem"),
    }
    output = Path(output)
    if origin_only and (output / "emoticon-lookup-front.conf").exists():
        raise ValueError("Use a fresh output directory for --origin-only; an old frontend config is present.")
    output.mkdir(parents=True, exist_ok=True)
    configs = {"emoticon-lookup-origin.conf": "origin.conf.template"}
    if not origin_only:
        configs["emoticon-lookup-front.conf"] = "https.conf.template" if https else "bootstrap.conf.template"
    for filename, template in configs.items():
        source = (TEMPLATES / template).read_text(encoding="utf-8")
        rendered = re.sub(r"@([A-Z_]+)@", lambda match: values[match.group(1)], source)
        (output / filename).write_text(rendered, encoding="utf-8", newline="\n")
    return list(configs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("generated/nginx"))
    parser.add_argument("--origin-port", type=int, default=8088)
    parser.add_argument("--web-root", default="/var/lib/emoticon-lookup/www")
    parser.add_argument("--acme-root", default="/var/lib/emoticon-lookup/acme")
    parser.add_argument("--https", action="store_true")
    parser.add_argument("--origin-only", action="store_true")
    parser.add_argument("--certificate")
    parser.add_argument("--certificate-key")
    parser.add_argument("--password-file", default="/etc/nginx/emoticon-lookup.htpasswd")
    args = parser.parse_args()
    try:
        names = render(args.output_dir, args.domain, args.origin_port, args.web_root,
                       args.acme_root, args.https, args.certificate, args.certificate_key,
                       args.password_file, args.origin_only)
    except (ValueError, OSError) as problem:
        parser.exit(1, f"Error: {problem}\n")
    for name in names:
        print(args.output_dir / name)


if __name__ == "__main__":
    main()
