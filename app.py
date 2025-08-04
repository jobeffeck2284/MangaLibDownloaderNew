import os
import io
import logging
import requests
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import threading
import time
import uuid
from PIL import Image as PILImage

# Настройка логгирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Заголовки для обхода 403 ошибки
HEADERS = {
    "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
}

app = Flask(__name__)
# Разрешить CORS для локальной разработки
CORS(app)

# Хранилище для данных о текущей выбранной манге и задачах скачивания
# В реальном приложении используйте базу данных или сессии
app_state = {
    'current_manga': None,
    'auth_token': '',
    'download_tasks': {}  # Хранение статуса загрузок
}

# Хранилище для результатов поиска (упрощенное)
search_results_cache = {}


@app.route('/api/set_token', methods=['POST'])
def set_token():
    """Установить токен авторизации"""
    data = request.get_json()
    token = data.get('token', '').strip()
    app_state['auth_token'] = token
    logging.info(f"Token set: {bool(token)}")
    return jsonify({'status': 'success', 'message': 'Токен установлен'})


@app.route('/api/search', methods=['GET'])
def search_manga():
    """Поиск манги"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Введите поисковый запрос'}), 400

    url = f"https://api.cdnlibs.org/api/manga?fields[]=rate_avg&fields[]=rate&fields[]=releaseDate&q={query}&site_id[]=1&site_id[]=4"
    try:
        logging.info(f"Searching for manga: {query}")
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = []
        for manga in data.get('data', []):
            manga_id = manga.get('id', '')
            rus_name = manga.get('rus_name', 'Нет названия')
            eng_name = manga.get('eng_name', 'No name')
            slug_url = manga.get('slug_url', '')
            cover_url = manga.get('cover', {}).get('default', '')

            site_id = manga.get('site', 0)
            site_name = "MangaLib" if site_id == 1 else "HentaiLib" if site_id == 4 else f"Unknown ({site_id})"

            results.append({
                'id': manga_id,
                'rus_name': rus_name,
                'eng_name': eng_name,
                'slug_url': slug_url,
                'cover_url': cover_url,  # Логируем URL обложки
                'site_id': site_id,
                'site_name': site_name
            })
            logging.debug(f"Found manga: {rus_name} (ID: {manga_id}) with cover URL: {cover_url}")

        # Кэшируем результаты для последующих запросов деталей
        cache_key = str(uuid.uuid4())
        search_results_cache[cache_key] = results
        logging.info(f"Search completed. Found {len(results)} results.")
        return jsonify({'status': 'success', 'results': results, 'cache_key': cache_key})
    except Exception as e:
        logging.error(f"Search error: {str(e)}")
        return jsonify({'error': f'Ошибка при поиске: {str(e)}'}), 500


@app.route('/api/manga/<slug_url>', methods=['GET'])
def get_manga_details(slug_url):
    """Получить детали манги"""
    site_id = int(request.args.get('site_id', 1))
    referer = "https://hentailib.me/" if site_id == 4 else "https://mangalib.me/"
    headers = HEADERS.copy()
    headers["Referer"] = referer

    try:
        logging.info(f"Fetching details for manga: {slug_url} (Site ID: {site_id})")
        url = f"https://api.cdnlibs.org/api/manga/{slug_url}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json().get('data', {})

        site_name = "MangaLib" if site_id == 1 else "HentaiLib" if site_id == 4 else f"Unknown ({site_id})"

        manga_info = {
            'slug_url': slug_url,
            'title': data.get('rus_name', ''),
            'type': data.get('type', {}).get('label', ''),
            'status': data.get('status', {}).get('label', ''),
            'cover_url': data.get('cover', {}).get('default', ''),
            'rating': data.get('rating', {}).get('averageFormated', 'N/A'),
            'site': site_name,
            'site_id': site_id,
            'referer': referer
        }

        logging.info(f"Manga details fetched: {manga_info['title']}")
        logging.debug(f"Manga cover URL: {manga_info['cover_url']}")

        app_state['current_manga'] = manga_info
        return jsonify({'status': 'success', 'manga': manga_info})
    except Exception as e:
        logging.error(f"Manga details error: {str(e)}")
        return jsonify({'error': f'Ошибка при загрузке деталей: {str(e)}'}), 500


@app.route('/api/manga/<slug_url>/chapters_info', methods=['GET'])
def get_chapters_info(slug_url):
    """Получить информацию о главах манги"""
    manga_data = app_state.get('current_manga')
    if not manga_data or manga_data['slug_url'] != slug_url:
        return jsonify({'error': 'Манга не выбрана или данные не совпадают'}), 400

    referer = manga_data['referer']
    site_id = manga_data['site_id']

    try:
        logging.info(f"Fetching chapters info for manga: {slug_url}")
        url = f"https://api.cdnlibs.org/api/manga/{slug_url}/chapters"
        headers = HEADERS.copy()
        headers["Referer"] = referer
        if app_state['auth_token']:
            headers["Authorization"] = f"Bearer {app_state['auth_token']}"

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        chapters = data.get('data', [])
        if not chapters:
            logging.warning("No chapters found for manga")
            return jsonify({'error': 'Не удалось получить информацию о главах'}), 404

        volumes = set()
        chapters_count = len(chapters)
        for chapter in chapters:
            volume = chapter.get('volume')
            if volume is not None:  # Volume может быть 0
                volumes.add(volume)
        volumes_count = len(volumes)

        # Сортируем главы по тому и номеру главы (как в оригинале)
        def sort_key(chapter):
            try:
                vol = float(chapter.get('volume', '0') or 0)
            except (ValueError, TypeError):
                vol = 0
            try:
                ch = float(chapter.get('number', '0') or 0)
            except (ValueError, TypeError):
                ch = 0
            return (vol, ch)

        sorted_chapters = sorted(chapters, key=sort_key)

        chapters_info = {
            'volumes': volumes_count,
            'chapters': chapters_count,
            'chapters_list': sorted_chapters  # Отправляем отсортированный список
        }
        logging.info(f"Chapters info fetched: {volumes_count} volumes, {chapters_count} chapters")
        return jsonify({'status': 'success', 'chapters_info': chapters_info})
    except Exception as e:
        logging.error(f"Chapters info error: {str(e)}")
        return jsonify({'error': f'Ошибка при загрузке информации о главах: {str(e)}'}), 500


@app.route('/api/download_chapter', methods=['POST'])
def start_download_chapter():
    """Инициировать скачивание главы"""
    if not app_state['current_manga']:
        return jsonify({'error': 'Сначала выберите мангу'}), 400

    data = request.get_json()
    volume = str(data.get('volume', '')).strip()
    chapter = str(data.get('chapter', '')).strip()

    if not volume or not chapter:
        return jsonify({'error': 'Введите номер тома и главы'}), 400

    manga = app_state['current_manga']
    slug_url = manga['slug_url']
    referer = manga['referer']
    site_id = manga['site_id']

    # Генерируем уникальный ID задачи
    task_id = str(uuid.uuid4())

    # Инициализируем статус задачи
    app_state['download_tasks'][task_id] = {
        'status': 'starting',
        'message': 'Начало загрузки...',
        'progress': 0,
        'total_pages': 0,
        'folder_path': None
    }

    # Запускаем скачивание в отдельном потоке
    thread = threading.Thread(target=download_chapter_worker,
                              args=(task_id, slug_url, volume, chapter, referer, site_id))
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'success', 'task_id': task_id, 'message': 'Загрузка начата'})


def download_chapter_worker(task_id, slug_url, volume, chapter, referer, site_id):
    """Функция для выполнения загрузки в отдельном потоке"""

    def update_status(status, message, progress=None, total_pages=None):
        task = app_state['download_tasks'].get(task_id)
        if task:
            task['status'] = status
            task['message'] = message
            if progress is not None:
                task['progress'] = progress
            if total_pages is not None:
                task['total_pages'] = total_pages

    try:
        update_status('fetching', 'Получение информации о главе...')
        logging.info(f"Starting download for chapter {chapter} volume {volume} of manga {slug_url}")

        # Получаем данные главы
        url = f"https://api.cdnlibs.org/api/manga/{slug_url}/chapter?number={chapter}&volume={volume}"
        headers = HEADERS.copy()
        headers["Referer"] = referer
        if app_state['auth_token']:
            headers["Authorization"] = f"Bearer {app_state['auth_token']}"

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        chapter_data = response.json().get('data', {})
        pages = chapter_data.get('pages', [])

        if not pages:
            update_status('error', 'Страницы главы не найдены')
            logging.warning("No pages found for chapter")
            return

        update_status('preparing', 'Создание папки для скачивания...', total_pages=len(pages))

        # Создаем папку для скачивания
        title = app_state['current_manga']['title']
        folder_name = f"{title}_Том_{volume}_Глава_{chapter}"
        os.makedirs(folder_name, exist_ok=True)
        folder_path = os.path.abspath(folder_name)

        app_state['download_tasks'][task_id]['folder_path'] = folder_path
        logging.info(f"Created download folder: {folder_path}")

        # Определяем домен для изображений
        img_domain = "https://img2h.imgslib.link" if site_id == 4 else "https://img33.imgslib.link"

        update_status('downloading', 'Начато скачивание страниц...', progress=0)

        for i, page in enumerate(pages, 1):
            try:
                img_url = img_domain + page['url']
                page_headers = HEADERS.copy()
                page_headers["Referer"] = referer
                if app_state['auth_token']:
                    page_headers["Authorization"] = f"Bearer {app_state['auth_token']}"

                img_response = requests.get(img_url, headers=page_headers, timeout=10)
                img_response.raise_for_status()

                page_num = page['slug']
                ext = os.path.splitext(page['image'])[1] or '.jpg'
                filename = os.path.join(folder_path, f"страница_{page_num}{ext}")

                with open(filename, 'wb') as f:
                    f.write(img_response.content)

                update_status('downloading', f'Скачана страница {i} из {len(pages)}', progress=i)
                logging.debug(f"Downloaded page {i}/{len(pages)}: {filename}")

            except Exception as e:
                logging.error(f"Error downloading page {i}: {str(e)}")
                update_status('error', f'Ошибка при скачивании страницы {i}: {str(e)}')
                return  # Прерываем загрузку при ошибке

        update_status('completed', 'Скачивание завершено!', progress=len(pages))
        logging.info("Chapter download completed successfully")

    except Exception as e:
        logging.error(f"Download error: {str(e)}")
        update_status('error', f'Ошибка при получении страниц: {str(e)}')


@app.route('/api/download_status/<task_id>', methods=['GET'])
def get_download_status(task_id):
    """Получить статус задачи скачивания"""
    task = app_state['download_tasks'].get(task_id)
    if not task:
        return jsonify({'error': 'Задача не найдена'}), 404
    return jsonify({'status': 'success', 'task': task})


# --- Добавленный маршрут для проверки логов (для отладки) ---
@app.route('/api/logs')
def get_logs():
    """(Опционально) Получить последние логи (для отладки в браузере)"""
    # В реальном приложении лучше использовать лог-файлы
    import sys
    import io

    # Этот маршрут не рекомендуется для продакшена
    # Просто для демонстрации логов в браузере
    # Вместо этого смотрите логи в консоли, где запущен Flask

    # Для демонстрации просто возвращаем сообщение
    return jsonify({'message': 'Смотрите логи в консоли, где запущен сервер Flask'})


if __name__ == '__main__':
    print("Запуск Flask API сервера...")
    print("Откройте index.html в браузере для использования приложения.")
    app.run(debug=True, port=5000)
