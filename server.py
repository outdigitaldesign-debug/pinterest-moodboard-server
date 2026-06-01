import subprocess, json, urllib.request, urllib.error, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == '/to-slides':
            # Читаем тело запроса от Figma-плагина
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)

            try:
                payload = json.loads(body)
                script_url = payload.get('scriptUrl')
                if not script_url:
                    self.respond(400, {'error': 'scriptUrl is required'})
                    return

                # Пересылаем в Google Apps Script
                req = urllib.request.Request(
                    script_url,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0',
                    },
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    response_body = r.read()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', len(response_body))
                self.end_headers()
                self.wfile.write(response_body)

            except urllib.error.HTTPError as e:
                err_body = e.read()
                self.respond(502, {'error': 'Apps Script error: ' + err_body.decode('utf-8', errors='replace')})
            except Exception as e:
                self.respond(500, {'error': str(e)})
            return

        self.respond(404, {'error': 'Not found'})

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == '/img':
            url = params.get('url', [None])[0]
            if not url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', len(data))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        url = params.get('url', [None])[0]
        if not url or 'pinterest.com' not in url:
            self.respond(400, {'error': 'Invalid URL'})
            return

        try:
            result = subprocess.run(
                ['python3', '-m', 'gallery_dl', '--dump-json', url],
                capture_output=True, text=True, timeout=60
            )
            data = json.loads(result.stdout)
            images = []
            for item in data:
                if not isinstance(item, list) or len(item) < 2:
                    continue
                entry = item[1]
                if not isinstance(entry, dict):
                    continue
                imgs = entry.get('images', {})
                orig = imgs.get('orig', {})
                img_url = orig.get('url', '')
                if not img_url:
                    continue
                section = ''
                bs = entry.get('board_section')
                if isinstance(bs, dict):
                    section = bs.get('title', '')
                pin_id = entry.get('id', '')
                pin_url = 'https://www.pinterest.com/pin/' + str(pin_id) + '/' if pin_id else ''
                is_video = entry.get('videos') is not None or entry.get('is_video', False) or img_url.endswith('.gif')
                images.append({
                    'url': img_url,
                    'section': section,
                    'width': orig.get('width', 736),
                    'height': orig.get('height', 1000),
                    'pin_url': pin_url,
                    'is_video': is_video
                })
            self.respond(200, {'images': images})
        except Exception as e:
            self.respond(500, {'error': str(e)})

    def respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass

port = int(os.environ.get('PORT', 8765))
print(f'Server running on port {port}')
HTTPServer(('0.0.0.0', port), Handler).serve_forever()
