import os
import random
import getpass
from robobrowser import RoboBrowser
import json
from pathlib import Path
from http.cookies import SimpleCookie
import requests

# codeforces.com sits behind Cloudflare, which fingerprints the TLS handshake:
# a plain requests session is answered with "HTTP 403 / Just a moment...".
# curl_cffi reproduces a real browser's TLS and HTTP/2 fingerprint.  RoboBrowser
# accepts a replacement session because it only calls session.request/get/post
# and reads session.cookies.
try:
    from curl_cffi import requests as _requests

    _IMPERSONATE = 'firefox147'
except ImportError:  # pragma: no cover - curl_cffi not installed
    _requests = requests

    _IMPERSONATE = None


def _mime_from(data, files):
    """Express requests' data=/files= pair as a curl_cffi CurlMime.

    RoboBrowser serializes <input type="file"> fields into files= -- its
    FileInput.payload_key is 'files' -- and requests accepts (name, file object)
    pairs there.  curl_cffi has no equivalent, so those parts have to be read and
    rebuilt as CurlMime parts.
    """
    from curl_cffi import CurlMime

    mime = CurlMime()
    for name, value in (data or []):
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ''
        mime.addpart(name=name,
                     data=value if isinstance(value, bytes) else str(value).encode())
    for name, item in (files or []):
        filename = content_type = None
        if isinstance(item, (list, tuple)):
            if len(item) >= 3:
                filename, body, content_type = item[0], item[1], item[2]
            elif len(item) == 2:
                filename, body = item
            else:
                body = item[0]
        else:
            body = item
        if hasattr(body, 'read'):
            if not filename:
                filename = os.path.basename(getattr(body, 'name', '') or '') or None
            try:
                body.seek(0)
            except (OSError, ValueError):
                pass
            payload = body.read()
        else:
            payload = body
        if isinstance(payload, str):
            payload = payload.encode()
        if filename is None and content_type is None:
            mime.addpart(name=name, data=payload)
        else:
            # requests sends no Content-Type for a file object handed to it
            # without an explicit type, so this must not invent one either.
            mime.addpart(name=name, data=payload, filename=filename, content_type=content_type)
    return mime


if _IMPERSONATE:
    class _FormSession(_requests.Session):
        """A curl_cffi session that also understands requests' files= spelling.

        RoboBrowser builds file inputs as files=, which curl_cffi rejects with
        "files is not supported, use `multipart`".  Without this translation the
        Codeforces source upload never leaves the client.
        """

        def request(self, method, url, *args, **kwargs):
            files = kwargs.pop('files', None)
            if files:
                kwargs['multipart'] = _mime_from(kwargs.pop('data', None), files)
            return super().request(method, url, *args, **kwargs)


def make_session(user_agent=None):
    """A browser session that can pass Cloudflare and accept RoboBrowser's forms."""
    if _IMPERSONATE:
        return _FormSession(impersonate=_IMPERSONATE)
    session = _requests.Session()
    if user_agent:
        session.headers['User-Agent'] = user_agent
    return session

root = '7'
""" converter """
def decode(s):
    global root
    res = ""
    length = len(s)
    i = 0
    while i < length:
        rng = ord(s[i])-ord(root)
        jump = ord(s[i+1])-ord(root)
        temp = 0
        for j in range (0, rng):
            temp += ord(s[i+j+2]) - ord(root) - jump
        res += str(chr(temp))
        i += rng + 2
    return res

def encode(s):
    global root
    res = ""
    length = len(s)
    for i in range (0, length):
        rng = random.randint(1, 20)
        res += str(chr(rng + ord(root)))
        jump = random.randint(1,10)
        res += str(chr(jump + ord(root)))
        curr = ord(s[i])
        for j in range (0, rng-1):
            temp = random.randint(0, min(curr, 2+int(curr/(rng-j))))
            res += str(chr(temp + ord(root) + jump))
            curr -= temp
        res += str(chr(curr + ord(root) + jump))
    return res

def get_secret(inclupass):
    context = Path('contest.json')
    if context.is_file():
        account = json.loads(context.read_text())
        if account.get('platform') == 'gym' and account.get('handle'):
            return (account['handle'], None) if inclupass else account['handle']
    handle = None
    password = None
    secret_loc = os.path.join(os.path.dirname(__file__), "secret")
    if os.path.isfile(secret_loc):
        secretfile = open(secret_loc, "r")
        rawdata = secretfile.read().rstrip('\n').split()
        handle = decode(rawdata[0])
        if inclupass:
            password = decode(rawdata[1])
        secretfile.close()
    if inclupass:
        return handle, password
    else:
        return handle

""" set login """
def set_login(handle=None):
    if handle is None:
        handle = input("Handle: ")
    password = getpass.getpass("Password: ")

    browser = RoboBrowser(parser = "lxml", session = make_session())
    browser.open("https://codeforces.com/enter")
    enter_form = browser.get_form("enterForm")
    enter_form["handleOrEmail"] = handle
    enter_form["password"] = password
    browser.submit_form(enter_form)

    checks = list(map(lambda x: x.getText()[1:].strip(),
        browser.select("div.caption.titled")))
    if handle not in checks:
        print("Login Failed.")
        return
    else:
        secret_loc = os.path.join(os.path.dirname(__file__), "secret")
        secretfile = open(secret_loc, "w")
        secretfile.write(encode(handle) + " " + encode(password))
        secretfile.close()
        print("Successfully logged in as " + handle)

""" login """
def login():
    context = Path('contest.json')
    if context.is_file():
        account = json.loads(context.read_text())
        if account.get('platform') == 'gym':
            session = make_session(account['user_agent'])
            jar = SimpleCookie()
            jar.load(account['cookie'])
            for item in jar.values():
                session.cookies.set(item.key, item.value, domain='codeforces.com', path='/')
            browser = RoboBrowser(parser='lxml', session=session)
            browser.open('https://codeforces.com/')
            logged_in = any(a.get('href', '').lower() == '/profile/' + account['handle'].lower()
                            for a in browser.select('#header a'))
            if not logged_in:
                raise SystemExit('Codeforces login cookie expired or belongs to another account.')
            return browser
    handle, password = get_secret(True)

    browser = RoboBrowser(parser = "lxml", session = make_session())
    browser.open("https://codeforces.com/enter")
    enter_form = browser.get_form("enterForm")
    enter_form["handleOrEmail"] = handle
    enter_form["password"] = password
    browser.submit_form(enter_form)

    checks = list(map(lambda x: x.getText()[1:].strip(), browser.select("div.caption.titled")))
    if handle not in checks:
        print("Login Corrupted.")
        return None
    else:
        return browser
