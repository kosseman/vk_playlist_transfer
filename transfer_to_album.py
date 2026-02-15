"""
Перенос всех аудиозаписей из «Мои аудиозаписи» ВК в новый плейлист.

Использует vk_api (парсинг m.vk.ru) — единственный способ получить полный список треков.
Официальный API ВК ограничен ~300 аудиозаписями.
"""

import configparser
import time
import webbrowser
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from vkpymusic import Service


VK_API_VERSION = "5.95"
VK_API_BASE = "https://api.vk.com/method"
BATCH_SIZE = 50
PLAYLIST_MAX = 1000  # Kate API audio.addToPlaylist тихо обрезает на 1000
CONFIG_FILE = "config_vk.ini"
MIN_TRACKS = 500  # Меньше = не создаём плейлист (API лимит)


def _load_service_from_config(config_path: Path) -> Service | None:
    config = configparser.ConfigParser()
    if not config_path.exists():
        return None
    config.read(config_path, encoding="utf-8")
    if "VK" not in config:
        return None
    token = config["VK"].get("token_for_audio") or config["VK"].get("token")
    user_agent = config["VK"].get("user_agent", "")
    if token and user_agent:
        return Service(user_agent, token)
    return None


def _get_credentials(service: Service) -> tuple[str, str]:
    try:
        return service.user_agent, service._Service__token
    except AttributeError:
        pass
    config_path = Path(__file__).parent / CONFIG_FILE
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    if "VK" in config:
        token = config["VK"].get("token_for_audio") or config["VK"].get("token")
        ua = config["VK"].get("user_agent", "")
        if token:
            return ua, token
    raise RuntimeError("Нет токена. Получите токен через браузер (см. ниже).")


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
)


class _TimeoutSession(requests.Session):
    """Сессия с увеличенным таймаутом и повторными попытками при ошибках."""
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 180)
        return super().request(*args, **kwargs)


def _session_with_retry() -> requests.Session:
    """Сессия с таймаутом 3 мин и повторными попытками при таймауте/ошибках."""
    session = _TimeoutSession()
    session.headers["User-Agent"] = BROWSER_USER_AGENT
    retry = Retry(total=5, read=5, connect=5, backoff_factor=3, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _load_cookies_session(cookie_path: Path) -> requests.Session:
    """Загружает cookies из файла Netscape и возвращает сессию."""
    session = _session_with_retry()
    jar = MozillaCookieJar(str(cookie_path))
    jar.load(ignore_discard=True, ignore_expires=True)
    session.cookies.update(jar)
    session.headers["User-Agent"] = BROWSER_USER_AGENT
    return session


def _fetch_all_via_vk_api(
    user_id: int,
    *,
    login: str | None = None,
    password: str | None = None,
    cookie_path: Path | None = None,
    kate_token: str | None = None,
) -> list[tuple[str, str]]:
    """Парсинг m.vk.ru — получает ВСЕ треки."""
    from vk_api import VkApi
    from vk_api.audio import VkAudio

    def two_factor_handler():
        code = input("Введите код из SMS или приложения 2FA: ").strip()
        return code, False

    if cookie_path and cookie_path.exists() and kate_token:
        session = _load_cookies_session(cookie_path)
        # Cookies для m.vk.ru + Kate-токен для API. auth() пропускаем —
        # он требует cookie p с login.vk.ru, которого нет в экспорте браузера.
        vk = VkApi(token=kate_token, session=session)
        # Не вызываем auth() — token уже есть, cookies в session
    else:
        vk = VkApi(
            login=login or "",
            password=password or "",
            session=_session_with_retry(),
            auth_handler=two_factor_handler,
        )
        vk.auth()

    audio = VkAudio(vk)
    print("Загрузка треков (время зависит от количества, прогресс каждые 100)...", flush=True)
    tracks = []
    for track in audio.get_iter(owner_id=user_id):
        oid = track.get("owner_id") if isinstance(track, dict) else getattr(track, "owner_id", None)
        tid = track.get("id") if isinstance(track, dict) else getattr(track, "id", None)
        if oid is not None and tid is not None:
            tracks.append((str(oid), str(tid)))
        if len(tracks) % 100 == 0 and tracks:
            print(f"  Загружено: {len(tracks)} треков...", flush=True)
    return tracks


def create_playlist(service: Service, owner_id: int, title: str) -> dict:
    user_agent, token = _get_credentials(service)
    params = {
        "access_token": token,
        "owner_id": owner_id,
        "title": title,
        "v": VK_API_VERSION,
    }
    resp = requests.get(
        f"{VK_API_BASE}/audio.createPlaylist?{urlencode(params)}",
        headers={"User-Agent": user_agent},
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Ошибка: [{data['error']['error_code']}] {data['error'].get('error_msg', '')}")
    return data["response"]


def add_to_playlist(service: Service, owner_id: int, playlist_id: int, audio_ids: list[str]) -> None:
    user_agent, token = _get_credentials(service)
    params = {
        "access_token": token,
        "owner_id": owner_id,
        "playlist_id": playlist_id,
        "audio_ids": ",".join(audio_ids),
        "v": VK_API_VERSION,
    }
    resp = requests.get(
        f"{VK_API_BASE}/audio.addToPlaylist?{urlencode(params)}",
        headers={"User-Agent": user_agent},
    )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Ошибка: [{data['error']['error_code']}] {data['error'].get('error_msg', '')}")


def main():
    print("=== Перенос всех аудиозаписей ВК в новый плейлист ===\n")

    config_path = Path(__file__).parent / CONFIG_FILE
    service = Service.parse_config() or _load_service_from_config(config_path)
    if service is None:
        print("Токен Kate нужен для добавления в плейлист. Авторизуйтесь в браузере:")
        auth_url = (
            "https://oauth.vk.com/authorize?"
            "client_id=2685278&display=page&"
            "redirect_uri=https://oauth.vk.com/blank.html&"
            "scope=audio,offline&response_type=token&v=5.199"
        )
        webbrowser.open(auth_url)
        print("Скопируйте access_token из адресной строки после входа.")
        token = input("Токен: ").strip()
        if not token:
            return
        from vkaudiotoken.supported_clients import KATE
        service = Service(KATE.user_agent, token)
        config = configparser.ConfigParser()
        config["VK"] = {"user_agent": KATE.user_agent, "token_for_audio": token}
        with open(config_path, "w", encoding="utf-8") as f:
            config.write(f)
        print("Токен сохранён.\n")

    user_info = service.get_user_info()
    user_id = user_info.userid
    print(f"Пользователь: {user_info.first_name} {user_info.last_name} (id: {user_id})\n")

    playlist_title = input("Название нового плейлиста: ").strip()
    if not playlist_title:
        return

    print("\nДля загрузки списка треков нужна авторизация:")
    print("  — Логин и пароль, либо")
    print("  — Cookies из браузера (если код 2FA не приходит, см. README)")
    print()
    use_cookies = input("Путь к cookies.txt (Enter = логин/пароль): ").strip()
    cookie_path = Path(use_cookies).expanduser() if use_cookies else None
    login = None
    password = None
    if not cookie_path or not cookie_path.exists():
        cookie_path = None
        print("Введите логин и пароль:")
        login = input("Логин: ").strip()
        password = input("Пароль: ").strip()
        if not login or not password:
            print("Логин и пароль обязательны.")
            return
    else:
        login = input("Логин (email или телефон): ").strip() or "cookies"

    kate_token = None
    if cookie_path:
        _, kate_token = _get_credentials(service)
        if not kate_token:
            print("Для cookies нужен Kate-токен в config_vk.ini.")
            return

    try:
        all_tracks = _fetch_all_via_vk_api(
            user_id,
            login=login,
            password=password,
            cookie_path=cookie_path,
            kate_token=kate_token,
        )
    except Exception as e:
        print(f"\nОшибка: {e}")
        print("Плейлист не создан.")
        return

    if len(all_tracks) < MIN_TRACKS:
        print(f"\nПолучено {len(all_tracks)} треков (минимум {MIN_TRACKS} для продолжения).")
        print("Официальный API ВК отдаёт только ~300. Используйте cookies (см. README).")
        return

    print(f"\nВсего треков: {len(all_tracks)}")
    all_tracks.reverse()

    audio_ids = [f"{o}_{t}" for o, t in all_tracks]
    print(f"\nСоздаю плейлист «{playlist_title}»...")
    playlist_resp = create_playlist(service, user_id, playlist_title)
    playlist_id = playlist_resp.get("id") or playlist_resp.get("playlist_id")
    if not playlist_id:
        print("Ошибка создания плейлиста.")
        return

    add_to_playlist(service, user_id, playlist_id, audio_ids[:BATCH_SIZE])
    total_added = BATCH_SIZE
    print(f"  Добавлено: {total_added}/{len(all_tracks)}", flush=True)
    time.sleep(0.5)

    for i in range(BATCH_SIZE, min(PLAYLIST_MAX, len(audio_ids)), BATCH_SIZE):
        batch = audio_ids[i : i + BATCH_SIZE]
        try:
            add_to_playlist(service, user_id, playlist_id, batch)
            total_added += len(batch)
            print(f"  Добавлено: {total_added}/{len(all_tracks)} в «{playlist_title}»", flush=True)
        except RuntimeError as e:
            try:
                time.sleep(2)
                add_to_playlist(service, user_id, playlist_id, batch)
                total_added += len(batch)
                print(f"  Добавлено: {total_added}/{len(all_tracks)} (повтор)", flush=True)
            except RuntimeError:
                print(f"  Ошибка: {e}", flush=True)
        time.sleep(0.5)

    num_playlists = 1
    for part in range(1, (len(audio_ids) + PLAYLIST_MAX - 1) // PLAYLIST_MAX):
        start = part * PLAYLIST_MAX
        chunk = audio_ids[start : start + PLAYLIST_MAX]
        title = f"{playlist_title} ({part + 1})"
        print(f"\nСоздаю плейлист «{title}»...")
        playlist_resp = create_playlist(service, user_id, title)
        playlist_id = playlist_resp.get("id") or playlist_resp.get("playlist_id")
        if not playlist_id:
            continue
        num_playlists += 1
        for j in range(0, len(chunk), BATCH_SIZE):
            batch = chunk[j : j + BATCH_SIZE]
            try:
                add_to_playlist(service, user_id, playlist_id, batch)
                total_added += len(batch)
                print(f"  Добавлено: {total_added}/{len(all_tracks)} в «{title}»", flush=True)
            except RuntimeError as e:
                try:
                    time.sleep(2)
                    add_to_playlist(service, user_id, playlist_id, batch)
                    total_added += len(batch)
                    print(f"  Добавлено: {total_added}/{len(all_tracks)} (повтор)", flush=True)
                except RuntimeError:
                    print(f"  Ошибка: {e}", flush=True)
            time.sleep(0.5)

    print(f"\nГотово! Перенесено {total_added} треков в {num_playlists} плейлист(ов).")


if __name__ == "__main__":
    main()
