import csv
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from urllib.parse import quote

import requests
from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
CAPTURES = DATA / 'capturas'
DB = DATA / 'historico.sqlite3'
ROSTER = DATA / 'alunos.csv'
CONFIG = ROOT / 'config.json'
DATA.mkdir(exist_ok=True)
CAPTURES.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

with CONFIG.open(encoding='utf-8') as f:
    conf = json.load(f)

HOSTS = {item['id']: item for item in conf['computadores'] if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['id'])}
INTERVAL = int(conf.get('intervalo_minutos', 5))
RETENTION = int(conf.get('retencao_dias', 7))
if not 1 <= INTERVAL <= 120 or not 1 <= RETENTION <= 365:
    raise RuntimeError('Intervalo e retencao invalidos em config.json')
BASE = conf.get('webapi_url', 'http://127.0.0.1:11080/api/v1').rstrip('/')
if BASE not in ('http://127.0.0.1:11080/api/v1', 'http://localhost:11080/api/v1') and not BASE.startswith('https://'):
    raise RuntimeError('Utilize HTTP apenas no localhost; para acesso remoto, HTTPS.')
KEY_NAME = conf.get('identidade', 'aluno')
KEY_PATH = Path(conf.get('chave_privada', 'C:/ProgramData/Veyon/keys/private/aluno/key'))
AUTH_UUID = '0c69b301-81b4-42d6-8fae-128cdd113314'
PANEL_PASSWORD = os.environ.get('VEYON_PANEL_PASSWORD', '')
if len(PANEL_PASSWORD) < 12:
    raise RuntimeError('Defina VEYON_PANEL_PASSWORD com pelo menos 12 caracteres antes de iniciar.')

stop_event = threading.Event()
lock = threading.RLock()
state = {'active': False, 'thread': None, 'last_run': None, 'errors': {}}

def connect_db():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    return db

with connect_db() as db:
    db.execute('''CREATE TABLE IF NOT EXISTS capturas (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      host_id TEXT NOT NULL, aluno_nome TEXT NOT NULL, login_mascara TEXT NOT NULL,
      capturado_em TEXT NOT NULL, caminho TEXT NOT NULL,
      tamanho_bytes INTEGER NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_capturas_tempo ON capturas(capturado_em)')
    db.commit()

def normalize_login(v):
    v = (v or '').strip().split('\\')[-1].split('@')[0]
    return ''.join(c for c in v if c.isdigit())

def mask_login(s):
    s = normalize_login(s)
    return ('***' + s[-3:]) if s else ''

def roster():
    result = {}
    if not ROSTER.exists():
        return result
    with ROSTER.open('r', encoding='utf-8-sig', newline='') as f:
        sample = f.read(1024)
        f.seek(0)
        delimiter = ';' if sample.count(';') >= sample.count(',') else ','
        for r in csv.DictReader(f, delimiter=delimiter):
            k = normalize_login(r.get('cpf') or r.get('login'))
            if k and len(k) == 11:
                result[k] = {'nome': (r.get('nome') or '').strip(), 'turma': (r.get('turma') or '').strip()}
    return result

class VeyonError(Exception):
    pass


def make_connection(host):
    if not KEY_PATH.is_file():
        raise VeyonError('Chave privada nao encontrada no caminho configurado')
    # Somente le da chave privada em memoria; nao registra nem devolve pela API.
    keydata = KEY_PATH.read_text(encoding='utf-8')
    api = requests.Session()
    api.trust_env = False  # nao encaminhar chave a proxies configurados no SO
    target = quote(host, safe='')
    url = f'{BASE}/authentication/{target}'
    payload = {'method': AUTH_UUID, 'credentials': {'keyname': KEY_NAME, 'keydata': keydata}}
    try:
        res = api.post(url, json=payload, timeout=(3, 15))
        res.raise_for_status()
        uid = res.json().get('connection-uid')
        if not uid:
            raise VeyonError('Autenticacao sem connection-uid')
    except requests.RequestException as exc:
        api.close()
        raise VeyonError(f'Falha de autenticacao HTTP: {type(exc).__name__}') from exc
    api.headers['Connection-Uid'] = uid
    return api, url


def fetch_host(host_id):
    host = HOSTS[host_id]['host']
    api, auth_url = make_connection(host)
    try:
        # Espera obrigatoria: a sessao Veyon fica pronta depois da autenticacao.
        ready = False
        for _ in range(8):
            r = api.get(f'{BASE}/session', timeout=(3, 8))
            r.raise_for_status()
            data = r.json()
            if int(data.get('sessionId', -1)) >= 0 and data.get('sessionHostName'):
                ready = True
                break
            time.sleep(1.5)
        if not ready:
            raise VeyonError('Sessao nao ficou pronta')

        r = api.get(f'{BASE}/user', timeout=(3, 8))
        r.raise_for_status()
        login = r.json().get('login') or ''
        record = roster().get(normalize_login(login), {})
        nome = record.get('nome') or 'Aluno nao cadastrado' if login else 'Sem login identificado'
        turma = record.get('turma', '')

        image = None
        for _ in range(5):
            r = api.get(f'{BASE}/framebuffer', params={'format': 'jpeg', 'quality': 70, 'width': 1280}, timeout=(3, 20))
            if r.status_code == 503:
                time.sleep(1.5)
                continue
            r.raise_for_status()
            if not r.content.startswith(b'\xff\xd8'):
                raise VeyonError('Framebuffer nao retornou JPEG')
            image = r.content
            break
        if image is None:
            raise VeyonError('Framebuffer indisponivel apos tentativas')
        timestamp = datetime.now()
        fname = f'{host_id}_{timestamp:%Y%m%d_%H%M%S}_{secrets.token_hex(4)}.jpg'
        path = CAPTURES / fname
        path.write_bytes(image)
        with connect_db() as db:
            db.execute('''INSERT INTO capturas (host_id,aluno_nome,login_mascara,capturado_em,caminho,tamanho_bytes)
                       VALUES (?,?,?,?,?,?)''',
                       (host_id, nome, mask_login(login), timestamp.isoformat(timespec='seconds'), fname, len(image)))
            db.commit()
        return {'nome': nome, 'turma': turma, 'bytes': len(image)}
    except requests.RequestException as exc:
        raise VeyonError(f'Erro de comunicacao com estacao: {type(exc).__name__}') from exc
    finally:
        try:
            api.delete(auth_url, timeout=(3, 5))
        except requests.RequestException:
            logging.warning('Nao foi possivel encerrar uma conexao Veyon de %s', host_id)
        api.close()


def cleanup():
    threshold = (datetime.now() - timedelta(days=RETENTION)).isoformat(timespec='seconds')
    with connect_db() as db:
        old = db.execute('SELECT caminho FROM capturas WHERE capturado_em < ?', (threshold,)).fetchall()
        for r in old:
            (CAPTURES / r['caminho']).unlink(missing_ok=True)
        db.execute('DELETE FROM capturas WHERE capturado_em < ?', (threshold,))
        db.commit()


def cycle(host_ids):
    cleanup()
    for host_id in host_ids:
        if stop_event.is_set():
            break
        try:
            fetch_host(host_id)
            with lock:
                state['errors'].pop(host_id, None)
        except Exception as e:
            logging.warning('Falha no computador %s: %s', host_id, type(e).__name__)
            with lock:
                state['errors'][host_id] = str(e)
    with lock:
        state['last_run'] = datetime.now().isoformat(timespec='seconds')


def worker(host_ids):
    try:
        while not stop_event.is_set():
            cycle(host_ids)
            if stop_event.wait(INTERVAL * 60):
                break
    finally:
        with lock:
            state['active'] = False
            state['thread'] = None


def logged_in(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper


def verify_csrf():
    if not hmac.compare_digest(str(request.form.get('csrf', '')), str(session.get('csrf', 'INVALID'))):
        abort(403)

@app.after_request
def security_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; form-action 'self'"
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        candidate = request.form.get('senha', '')
        if hmac.compare_digest(candidate, PANEL_PASSWORD):
            session.clear()
            session['authenticated'] = True
            session['csrf'] = secrets.token_urlsafe(24)
            return redirect(url_for('index'))
        time.sleep(0.6)
        flash('Senha incorreta.')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
@logged_in
def logout():
    verify_csrf()
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@logged_in
def index():
    with connect_db() as db:
        items = db.execute('''SELECT c.* FROM capturas c INNER JOIN
        (SELECT host_id, MAX(id) id FROM capturas GROUP BY host_id) m ON c.id=m.id
        ORDER BY c.host_id''').fetchall()
    latest = {row['host_id']: row for row in items}
    with lock:
        snapshot = {'active': state['active'], 'last_run': state['last_run'], 'errors': state['errors'].copy()}
    return render_template('index.html', hosts=HOSTS, latest=latest, status=snapshot,
                           intervalo=INTERVAL, retencao=RETENTION, roster_count=len(roster()))

@app.route('/iniciar', methods=['POST'])
@logged_in
def start():
    verify_csrf()
    selected = request.form.getlist('host')
    if not selected or any(v not in HOSTS for v in selected):
        abort(400)
    if request.form.get('autorizado') != 'sim':
        flash('Confirme a autorizacao institucional e a ciencia dos usuarios antes de iniciar.')
        return redirect(url_for('index'))
    with lock:
        if state['active']:
            flash('Monitoramento ja esta ativo.')
        else:
            stop_event.clear()
            state['active'] = True
            state['errors'] = {}
            th = threading.Thread(target=worker, args=(tuple(selected),), daemon=True)
            state['thread'] = th
            th.start()
            flash('Monitoramento iniciado para as estacoes selecionadas.')
    return redirect(url_for('index'))

@app.route('/parar', methods=['POST'])
@logged_in
def stop():
    verify_csrf()
    stop_event.set()
    flash('Parada solicitada; a operacao em curso sera concluida antes de encerrar.')
    return redirect(url_for('index'))

@app.route('/historico')
@logged_in
def history():
    host_id = request.args.get('host', '')
    if host_id and host_id not in HOSTS:
        abort(400)
    with connect_db() as db:
        if host_id:
            entries = db.execute('SELECT * FROM capturas WHERE host_id=? ORDER BY id DESC LIMIT 200', (host_id,)).fetchall()
        else:
            entries = db.execute('SELECT * FROM capturas ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('historico.html', entries=entries, hosts=HOSTS, selected=host_id)

@app.route('/captura/<int:entry_id>')
@logged_in
def image(entry_id):
    with connect_db() as db:
        item = db.execute('SELECT caminho FROM capturas WHERE id=?', (entry_id,)).fetchone()
    if not item:
        abort(404)
    path = CAPTURES / item['caminho']
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype='image/jpeg', as_attachment=False)

if __name__ == '__main__':
    print('Painel local: http://127.0.0.1:5050')
    app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False, threaded=True)
