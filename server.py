import subprocess, json, urllib.request, urllib.error, urllib.parse, os, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# OAuth credentials из environment variables
CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
REDIRECT_URI  = os.environ.get('GOOGLE_REDIRECT_URI', 'https://pinterest-moodboard-server.onrender.com/oauth/callback')
SCOPES        = 'https://www.googleapis.com/auth/presentations https://www.googleapis.com/auth/drive'

# Хранилище токенов (в памяти — при рестарте сервера нужно будет переавторизоваться)
token_store = {'access_token': None, 'refresh_token': None}

def get_access_token():
    """Получаем актуальный access token, обновляем если истёк"""
    if not token_store['refresh_token']:
        return None
    # Обновляем access token через refresh token
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': token_store['refresh_token'],
        'grant_type': 'refresh_token'
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data)
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
        token_store['access_token'] = result['access_token']
        return result['access_token']

def slides_batch_update(presentation_id, requests, token):
    """Выполняем batchUpdate через REST API"""
    url = f'https://slides.googleapis.com/v1/presentations/{presentation_id}:batchUpdate'
    body = json.dumps({'requests': requests}).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def get_presentation(presentation_id, token):
    url = f'https://slides.googleapis.com/v1/presentations/{presentation_id}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def upload_image_to_drive(image_bytes, filename, token):
    """Загружаем картинку на Drive и получаем публичный URL"""
    # Сначала создаём файл
    metadata = json.dumps({'name': filename, 'mimeType': 'image/jpeg'}).encode()
    boundary = b'boundary12345'
    body = (
        b'--' + boundary + b'\r\n'
        b'Content-Type: application/json\r\n\r\n' + metadata + b'\r\n'
        b'--' + boundary + b'\r\n'
        b'Content-Type: image/jpeg\r\n\r\n' + image_bytes + b'\r\n'
        b'--' + boundary + b'--'
    )
    req = urllib.request.Request(
        'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
        data=body,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/related; boundary=boundary12345'
        }
    )
    with urllib.request.urlopen(req) as r:
        file_data = json.loads(r.read())
        file_id = file_data['id']

    # Делаем файл публичным
    perm_req = urllib.request.Request(
        f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions',
        data=json.dumps({'role': 'reader', 'type': 'anyone'}).encode(),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    )
    urllib.request.urlopen(perm_req)

    return f'https://drive.google.com/uc?id={file_id}'

def build_slide(payload, token):
    presentation_id = payload['presentationId']
    slide_number    = int(payload.get('templateSlideIndex', 1))
    template_index  = slide_number - 1
    sections        = payload['sections']

    # Получаем презентацию
    pres = get_presentation(presentation_id, token)
    slides = pres['slides']
    slide_w = pres['pageSize']['width']['magnitude']   # в EMU
    slide_h = pres['pageSize']['height']['magnitude']  # в EMU

    # Дублируем шаблонный слайд
    template_id = slides[template_index]['objectId']
    dup_result = slides_batch_update(presentation_id, [{
        'duplicateObject': {
            'objectId': template_id,
            'objectIds': {}
        }
    }], token)

    # Перемещаем новый слайд в конец
    new_slide_id = dup_result['replies'][0]['duplicateObject']['objectId']
    pres2 = get_presentation(presentation_id, token)
    new_index = len(pres2['slides']) - 1
    # Находим текущую позицию нового слайда
    for i, s in enumerate(pres2['slides']):
        if s['objectId'] == new_slide_id:
            current_index = i
            break

    if current_index != new_index:
        slides_batch_update(presentation_id, [{
            'updateSlidesPosition': {
                'slideObjectIds': [new_slide_id],
                'insertionIndex': new_index
            }
        }], token)

    # Удаляем все элементы с нового слайда
    pres3 = get_presentation(presentation_id, token)
    new_slide = next(s for s in pres3['slides'] if s['objectId'] == new_slide_id)
    delete_requests = []
    for el in new_slide.get('pageElements', []):
        delete_requests.append({'deleteObject': {'objectId': el['objectId']}})
    if delete_requests:
        slides_batch_update(presentation_id, delete_requests, token)

    # Координаты зон (в EMU)
    # Коэффициент: слайд реально 9144000x5143500 EMU = 720x405 в units Apps Script
    # Из наших замеров: 1 unit = 12700 EMU (9144000/720=12700)
    def u(val): return int(val * 12700)  # units → EMU

    left_x = u(22.5); left_y = u(68.25); left_w = u(262.5); left_h = u(315)
    right_x = u(292.5); right_y = u(68.25); right_w = u(405); right_h = u(315)
    gap = u(7.5)
    pad = u(10)

    requests = []
    id_counter = [1000]

    def new_id():
        id_counter[0] += 1
        return f'el{id_counter[0]}'

    # === ТЕКСТОВЫЙ ШЕЙП ===
    text_shape_id = new_id()
    requests.append({
        'createShape': {
            'objectId': text_shape_id,
            'shapeType': 'ROUND_RECTANGLE',
            'elementProperties': {
                'pageObjectId': new_slide_id,
                'size': {'width': {'magnitude': left_w, 'unit': 'EMU'}, 'height': {'magnitude': left_h, 'unit': 'EMU'}},
                'transform': {'scaleX': 1, 'scaleY': 1, 'translateX': left_x, 'translateY': left_y, 'unit': 'EMU'}
            }
        }
    })

    # Заливка белая
    requests.append({
        'updateShapeProperties': {
            'objectId': text_shape_id,
            'shapeProperties': {
                'shapeBackgroundFill': {
                    'solidFill': {'color': {'rgbColor': {'red': 1, 'green': 1, 'blue': 1}}, 'alpha': 1}
                },
                'outline': {
                    'outlineFill': {'solidFill': {'color': {'rgbColor': {'red': 0.129, 'green': 0.125, 'blue': 0.125}}, 'alpha': 1}},
                    'weight': {'magnitude': 2, 'unit': 'PT'},
                    'dashStyle': 'SOLID',
                    'propertyState': 'RENDERED'
                },
                'contentAlignment': 'TOP',
                'adjustments': {'autofit': None, 'value': 0.08}
            },
            'fields': 'shapeBackgroundFill,outline,contentAlignment'
        }
    })

    # Собираем текст секций
    section_h = left_h // len(sections)
    full_text = ''
    title_ranges = []  # [{start, end}]
    desc_ranges = []

    for i, s in enumerate(sections):
        if i > 0:
            full_text += '\n'
        t_start = len(full_text)
        full_text += s.get('name', '')
        t_end = len(full_text)
        title_ranges.append({'start': t_start, 'end': t_end})
        desc = s.get('description', '')
        if desc:
            full_text += '\n'
            d_start = len(full_text)
            full_text += desc
            d_end = len(full_text)
            desc_ranges.append({'start': d_start, 'end': d_end})
        if i < len(sections) - 1:
            full_text += '\n'

    # Вставляем текст
    requests.append({
        'insertText': {
            'objectId': text_shape_id,
            'insertionIndex': 0,
            'text': full_text
        }
    })

    # Базовый стиль — Roboto 9pt
    requests.append({
        'updateTextStyle': {
            'objectId': text_shape_id,
            'textRange': {'type': 'ALL'},
            'style': {
                'fontFamily': 'Roboto',
                'fontSize': {'magnitude': 9, 'unit': 'PT'},
                'bold': False,
                'foregroundColor': {'opaqueColor': {'rgbColor': {'red': 0.129, 'green': 0.125, 'blue': 0.125}}}
            },
            'fields': 'fontFamily,fontSize,bold,foregroundColor'
        }
    })

    # Выравнивание по левому для всего текста
    requests.append({
        'updateParagraphStyle': {
            'objectId': text_shape_id,
            'textRange': {'type': 'ALL'},
            'style': {'alignment': 'START', 'spaceAbove': {'magnitude': 0, 'unit': 'PT'}, 'spaceBelow': {'magnitude': 0, 'unit': 'PT'}},
            'fields': 'alignment,spaceAbove,spaceBelow'
        }
    })

    # Стиль заголовков — Unbounded Bold 12pt с зелёным фоном
    for tr in title_ranges:
        requests.append({
            'updateTextStyle': {
                'objectId': text_shape_id,
                'textRange': {'type': 'FIXED_RANGE', 'startIndex': tr['start'], 'endIndex': tr['end']},
                'style': {
                    'fontFamily': 'Unbounded',
                    'fontSize': {'magnitude': 12, 'unit': 'PT'},
                    'bold': True,
                    'foregroundColor': {'opaqueColor': {'rgbColor': {'red': 0.129, 'green': 0.125, 'blue': 0.125}}},
                    'backgroundColor': {'opaqueColor': {'rgbColor': {'red': 0.839, 'green': 1.0, 'blue': 0.412}}}
                },
                'fields': 'fontFamily,fontSize,bold,foregroundColor,backgroundColor'
            }
        })

    # === БЕНТО СЕТКА ===
    all_images = []
    for s in sections:
        all_images.extend(s.get('images', []))

    cols = 2 if len(all_images) <= 3 else 3 if len(all_images) <= 9 else 4
    col_w = int((right_w - gap * (cols - 1)) / cols)
    col_heights = [0] * cols
    col_x = [right_x + c * (col_w + gap) for c in range(cols)]

    for k, img in enumerate(all_images[:12]):
        shortest = col_heights.index(min(col_heights))
        ratio = img['width'] / img['height'] if img.get('height') else 1
        img_h = int(col_w / ratio)
        img_h = max(u(40), min(img_h, u(200)))

        img_x = col_x[shortest]
        img_y = right_y + col_heights[shortest]

        if col_heights[shortest] > right_h + u(50):
            continue

        # Загружаем картинку на Drive
        try:
            img_bytes = base64.b64decode(img['base64'])
            img_url = upload_image_to_drive(img_bytes, f'img_{k}.jpg', token)

            img_id = new_id()
            requests.append({
                'createImage': {
                    'objectId': img_id,
                    'url': img_url,
                    'elementProperties': {
                        'pageObjectId': new_slide_id,
                        'size': {'width': {'magnitude': col_w, 'unit': 'EMU'}, 'height': {'magnitude': img_h, 'unit': 'EMU'}},
                        'transform': {'scaleX': 1, 'scaleY': 1, 'translateX': img_x, 'translateY': img_y, 'unit': 'EMU'}
                    }
                }
            })

            # Обводка на картинку
            requests.append({
                'updateImageProperties': {
                    'objectId': img_id,
                    'imageProperties': {
                        'outline': {
                            'outlineFill': {'solidFill': {'color': {'rgbColor': {'red': 0.129, 'green': 0.125, 'blue': 0.125}}, 'alpha': 1}},
                            'weight': {'magnitude': 2, 'unit': 'PT'},
                            'dashStyle': 'SOLID',
                            'propertyState': 'RENDERED'
                        }
                    },
                    'fields': 'outline'
                }
            })

            col_heights[shortest] += img_h + gap
        except Exception as e:
            print(f'Image error {k}: {e}')

    # Отправляем все requests одним batchUpdate
    slides_batch_update(presentation_id, requests, token)
    return {'success': True}


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
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                token = get_access_token()
                if not token:
                    self.respond(401, {'error': 'Not authorized. Visit /oauth/start first.'})
                    return
                result = build_slide(payload, token)
                self.respond(200, result)
            except Exception as e:
                self.respond(500, {'error': str(e)})
            return

        self.respond(404, {'error': 'Not found'})

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # OAuth: начало авторизации
        if parsed.path == '/oauth/start':
            auth_url = (
                'https://accounts.google.com/o/oauth2/v2/auth?'
                f'client_id={CLIENT_ID}&'
                f'redirect_uri={urllib.parse.quote(REDIRECT_URI)}&'
                'response_type=code&'
                f'scope={urllib.parse.quote(SCOPES)}&'
                'access_type=offline&'
                'prompt=consent'
            )
            self.send_response(302)
            self.send_header('Location', auth_url)
            self.end_headers()
            return

        # OAuth: callback после авторизации
        if parsed.path == '/oauth/callback':
            code = params.get('code', [None])[0]
            if not code:
                self.respond(400, {'error': 'No code'})
                return
            try:
                data = urllib.parse.urlencode({
                    'code': code,
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'grant_type': 'authorization_code'
                }).encode()
                req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data)
                with urllib.request.urlopen(req) as r:
                    result = json.loads(r.read())
                    token_store['access_token'] = result.get('access_token')
                    token_store['refresh_token'] = result.get('refresh_token')

                body = b'<html><body><h2>OK! Authorization successful. You can close this tab.</h2></body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.respond(500, {'error': str(e)})
            return

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

        if parsed.path != '/':
            self.respond(404, {'error': 'Not found'})
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
