#!/usr/bin/env python3
from __future__ import annotations
import ssl, sys, urllib.request
from pathlib import Path
VENDOR = Path(__file__).resolve().parents[1] / 'app' / 'web' / 'static' / 'vendor'
MIN = 10000
FILES = {
  'bootstrap.min.css': [
    'https://unpkg.com/bootstrap@5.3.3/dist/css/bootstrap.min.css',
    'https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.3/css/bootstrap.min.css',
  ],
  'bootstrap.bundle.min.js': [
    'https://unpkg.com/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.3/js/bootstrap.bundle.min.js',
  ],
  'alpine.min.js': [
    'https://unpkg.com/alpinejs@3.14.3/dist/cdn.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.14.3/cdn.min.js',
  ],
  'socket.io.min.js': [
    'https://unpkg.com/socket.io-client@4.7.5/dist/socket.io.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js',
  ],
}
def grab(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'meeting-ai-vendor'})
    with urllib.request.urlopen(req, timeout=45, context=ssl.create_default_context()) as r:
        return r.read()
def main():
    VENDOR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, urls in FILES.items():
        dest = VENDOR / name
        if dest.is_file() and dest.stat().st_size > MIN:
            print(dest.stat().st_size, dest)
            continue
        data = None
        for u in urls:
            try:
                data = grab(u)
            except Exception as e:
                print('fail', u, e)
                continue
            if data and len(data) > MIN and not data.lstrip().startswith(b'<'):
                dest.write_bytes(data)
                break
        size = dest.stat().st_size if dest.is_file() else 0
        print(size, dest)
        if size <= MIN:
            ok = False
    return 0 if ok else 1
if __name__ == '__main__':
    sys.exit(main())
