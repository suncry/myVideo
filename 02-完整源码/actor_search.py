"""Custom browser search templates; actor names are encoded as a single value."""
from urllib.parse import quote, urlsplit

DEFAULT_TEMPLATE = "https://www.baidu.com/s?wd=<name>"
SETTING_KEY = "actor_search_template"


def search_url(template: str, name: str) -> str:
    template, name = template.strip(), name.strip()
    if not name:
        raise ValueError("演员姓名为空，无法搜索。")
    if "<name>" not in template:
        raise ValueError("请在网址中加入 <name>，用它代表演员姓名。")
    if any(char.isspace() or ord(char) < 32 for char in template):
        raise ValueError("网址中不能包含空格或换行。")
    try:
        parts = urlsplit(template)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        valid = valid and "<name>" not in parts.netloc and not parts.username and not parts.password
        parts.port  # Reject malformed port numbers before opening the browser.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请填写完整的 http:// 或 https:// 网址，将 <name> 放在路径或查询参数中。")
    if "<" in template.replace("<name>", "") or ">" in template.replace("<name>", ""):
        raise ValueError("姓名占位符请使用 <name>。")
    return template.replace("<name>", quote(name, safe=""))


def settings_path():
    """A single app preference store, outside both content libraries."""
    import privacy
    import app as core
    if privacy.BASE is None:
        return core.DATA_DIR / 'app-settings.sqlite3'
    return privacy.BASE.with_name(privacy.BASE.name + '-Settings.sqlite3')


def _legacy_templates():
    import sqlite3
    from contextlib import closing
    import privacy
    import app as core
    paths = [core.DB_PATH]
    if privacy.BASE is not None:
        stores = privacy.library_paths(privacy.BASE)
        paths = [stores['public'] / 'film_library.db', stores['private'] / 'film_library.db']
    values = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as conn:
                row = conn.execute('SELECT value FROM settings WHERE key=?', (SETTING_KEY,)).fetchone()
                if row and row[0].strip():
                    values.append(row[0].strip())
        except sqlite3.Error:
            continue
    return next((value for value in values if value != DEFAULT_TEMPLATE), DEFAULT_TEMPLATE)


def _connect_settings():
    import sqlite3
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    path.chmod(0o600)
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return conn


def load_template():
    from contextlib import closing
    with closing(_connect_settings()) as conn, conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (SETTING_KEY,)).fetchone()
        if row:
            return row[0]
        value = _legacy_templates()
        conn.execute('INSERT INTO settings VALUES(?,?)', (SETTING_KEY, value))
        return value


def save_template(template):
    from contextlib import closing
    template = template.strip()
    search_url(template, 'Example Name')
    with closing(_connect_settings()) as conn, conn:
        conn.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (SETTING_KEY, template))
