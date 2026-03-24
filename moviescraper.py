from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://www.filmweb.pl/search#/film?page={page}"
FILMWEB_ORIGIN = "https://www.filmweb.pl"

USER_AGENTS = [
    # Desktop Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Desktop Chrome (Linux)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def _small_human_delay(a: float = 0.25, b: float = 0.9) -> None:
    time.sleep(random.uniform(a, b))


def _normalize_film_url(href: str) -> Optional[str]:
    if not href:
        return None

    absolute = urljoin(FILMWEB_ORIGIN, href)
    parsed = urlparse(absolute)

    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc and parsed.netloc != urlparse(FILMWEB_ORIGIN).netloc:
        return None
    if "/film/" not in parsed.path:
        return None

    # Odrzucamy “poboczne” podstrony filmu (opinie, vod itp.)
    if any(seg in parsed.path for seg in ["/opinie", "/vod", "/ranking", "/person", "/serial", "/game", "/news"]):
        return None

    # Dla wyników wyszukiwania chcemy głównie “root” profilu filmu:
    # /film/<slug>-<year>-<id>
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) != 2 or path_parts[0] != "film":
        return None
    if not re.search(r"-\d+$", path_parts[1]):
        return None

    # Usuwamy fragmenty i query, żeby łatwiej deduplikować.
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _setup_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=pl-PL")
    options.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)

    # Minimalne “anti-detection” po stronie JS (bez zewnętrznych bibliotek).
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """
        },
    )
    return driver


def _try_close_didomi(driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
    # Didomi często ma przycisk o ID, ale czasem renderuje się chwilę później.
    try:
        btn = wait.until(EC.element_to_be_clickable((By.ID, "didomi-notice-agree-button")))
        btn.click()
        print("[DEBUG] Zamknięto popup cookies (Didomi).")
        return True
    except TimeoutException:
        return False
    except Exception as e:
        print(f"[DEBUG] Popup cookies wykryty, ale nie udało się kliknąć: {e!r}")
        return False


def _dump_debug_page(driver: webdriver.Chrome, label: str) -> None:
    """
    Zrzuca HTML + screenshot, żeby zobaczyć co faktycznie renderuje SPA / blokada.
    Pliki trafiają do ./debug/.
    """
    debug_dir = Path("debug")
    debug_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_")[:60] or "debug"
    base = debug_dir / f"{ts}_{safe_label}"

    try:
        html_path = str(base.with_suffix(".html"))
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")

        png_path = str(base.with_suffix(".png"))
        driver.save_screenshot(png_path)

        meta_path = str(base.with_suffix(".meta.txt"))
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"url={driver.current_url}\n")
            f.write(f"title={driver.title}\n")

        print(f"[DEBUG] Zapisano debug HTML: {html_path}")
        print(f"[DEBUG] Zapisano debug screenshot: {png_path}")
        print(f"[DEBUG] Zapisano debug meta: {meta_path}")
    except Exception as e:
        print(f"[DEBUG] Nie udało się zapisać debug artefaktów: {e!r}")


def _wait_for_search_results_ready(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """
    Filmweb to SPA – czasem <main> albo linki pojawiają się z opóźnieniem.
    Czekamy na: istnienie <main> oraz (link /film/ w <main> LUB sensowny kandydat kontenera wyników).
    """
    # W HTML Filmwebu na tej podstronie nie ma <main> – trzymamy się “page__content”.
    wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(@class,'page__content')]")))

    def _ready(drv: webdriver.Chrome) -> bool:
        try:
            # Linki do filmów w głównej siatce/listingu.
            if drv.find_elements(
                By.XPATH,
                "//*[contains(@class,'page__content')]//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]",
            ):
                return True
            # fallback: jakikolwiek “kandydat” kontenera wyników z co najmniej kilkoma linkami
            cands = drv.find_elements(
                By.XPATH,
                (
                    "//*[contains(@class,'page__content')]"
                    "//*[self::section or self::div or self::ul or self::ol]"
                    "[not(ancestor::header) and not(ancestor::footer)]"
                    "[count(.//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]) >= 5]"
                ),
            )
            return len(cands) > 0
        except Exception:
            return False

    wait.until(lambda d: _ready(d))


def _find_main_results_container(driver: webdriver.Chrome) -> Optional[object]:
    """
    Krytyczne: wybieramy kontener wyników wyszukiwania wewnątrz <main> i ignorujemy sidebar/nav/footer.
    Heurystyka: wybieramy element (section/div/ul/ol) z największą liczbą linków do /film/,
    o ile nie jest w nav/aside/footer i jest widoczny.
    """
    # Wg dumpa HTML: wyniki siedzą w bloku `page__content` (bez <main>).
    candidates = driver.find_elements(
        By.XPATH,
        (
            "//*[contains(@class,'page__content')]"
            "//*[self::section or self::div or self::ul or self::ol]"
            "[not(ancestor::header) and not(ancestor::footer)]"
            "[.//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]]"
        ),
    )

    best = None
    best_count = 0

    for el in candidates:
        try:
            if not el.is_displayed():
                continue
            film_links = el.find_elements(
                By.XPATH,
                ".//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]",
            )
            count = len(film_links)
            if count > best_count:
                best = el
                best_count = count
        except Exception:
            continue

    if best is not None:
        print(f"[DEBUG] Wybrano główny kontener wyników. Liczba linków /film/ w kontenerze: {best_count}")
    else:
        print("[DEBUG] Nie udało się jednoznacznie znaleźć kontenera wyników w <main>.")

    return best


def _collect_movie_links_from_results(driver: webdriver.Chrome) -> list[str]:
    container = _find_main_results_container(driver)
    if container is None:
        # Fallback awaryjny: tylko z `page__content`.
        anchors = driver.find_elements(
            By.XPATH,
            "//*[contains(@class,'page__content')]//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]",
        )
    else:
        anchors = container.find_elements(
            By.XPATH,
            ".//a[starts-with(@href, '/film/') and not(contains(@href, '/opinie'))]",
        )

    urls: set[str] = set()
    for a in anchors:
        href = a.get_attribute("href")
        normalized = _normalize_film_url(href or "")
        if normalized:
            urls.add(normalized)

    filtered = sorted(urls)

    print(f"[DEBUG] Zebrano {len(filtered)} unikalnych linków do filmów z kontenera wyników.")
    if filtered:
        print("[DEBUG] Przykładowe linki (max 5):")
        for u in filtered[:5]:
            print(" -", u)

    return filtered


def _extract_from_json_ld(driver: webdriver.Chrome) -> dict:
    data: dict = {}
    scripts = driver.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']")
    for s in scripts:
        try:
            raw = s.get_attribute("innerText") or ""
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            # Czasem jest lista obiektów JSON-LD.
            objs = obj if isinstance(obj, list) else [obj]
            for o in objs:
                if not isinstance(o, dict):
                    continue
                if "name" in o and "filmweb" in (driver.current_url or ""):
                    data.setdefault("name", o.get("name"))
                agg = o.get("aggregateRating") if isinstance(o.get("aggregateRating"), dict) else None
                if agg and "ratingValue" in agg:
                    data.setdefault("user_rating", str(agg.get("ratingValue")))
        except Exception:
            continue
    return data


@dataclass
class MovieRow:
    title: str
    user_rating: str
    critics_rating: str
    url: str


def _scrape_movie_page(driver: webdriver.Chrome, wait: WebDriverWait, url: str) -> MovieRow:
    driver.get(url)

    # Czekamy na tytuł.
    wait.until(EC.presence_of_element_located((By.XPATH, "//h1")))

    json_ld = _extract_from_json_ld(driver)

    # Tytuł
    title = "Brak"
    try:
        h1 = driver.find_element(By.XPATH, "//h1")
        title = (h1.text or "").strip() or title
    except Exception:
        pass
    if title == "Brak":
        try:
            og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:title']")
            title = (og.get_attribute("content") or "").strip() or title
        except Exception:
            pass
    if title == "Brak" and isinstance(json_ld.get("name"), str):
        title = json_ld["name"].strip() or title

    # Ocena użytkowników
    user_rating = "Brak"
    # Próby “DOM-first” (różne layouty / A/B testy)
    dom_user_xpaths = [
        # Często rating jest w blokach obok gwiazdek/liczb
        "//*[contains(@class,'filmRating') or contains(@class,'rating')][.//text()[contains(.,'Ocena')]]//*[self::span or self::div][normalize-space()!=''][1]",
        # Fallback: pierwsza liczba w okolicach słów "użytkowników"
        "//*[contains(translate(., 'UŻYTKOWNIKÓW', 'użytkowników'),'użytkowników')]/following::*[1]",
    ]
    for xp in dom_user_xpaths:
        try:
            el = driver.find_element(By.XPATH, xp)
            txt = (el.text or "").strip()
            m = re.search(r"\b\d([.,]\d)?\b", txt)
            if m:
                user_rating = m.group(0).replace(",", ".")
                break
        except Exception:
            continue
    if user_rating == "Brak" and isinstance(json_ld.get("user_rating"), str):
        user_rating = json_ld["user_rating"].strip() or user_rating

    # Ocena krytyków (czasem brak)
    critics_rating = "Brak"
    dom_crit_xpaths = [
        # Szukamy etykiety “krytyków” i bierzemy najbliższą liczbę obok/poniżej
        (
            "//*[contains(translate(., 'KRYTYKÓW', 'krytyków'),'krytyków') "
            "or contains(translate(., 'KRYTYCY', 'krytycy'),'krytycy')]"
            "/following::*[self::span or self::div][1]"
        ),
        # Alternatywnie: jakikolwiek widoczny element zawierający "krytyków" i liczbę w tym samym bloku
        "//*[contains(translate(., 'KRYTYKÓW', 'krytyków'),'krytyków') and (self::div or self::section)]",
    ]
    for xp in dom_crit_xpaths:
        try:
            el = driver.find_element(By.XPATH, xp)
            txt = (el.text or "").strip()
            m = re.search(r"\b\d([.,]\d)?\b", txt)
            if m:
                critics_rating = m.group(0).replace(",", ".")
                break
        except Exception:
            continue

    print(
        f"[DEBUG] Film: {title!r} | użytkownicy={user_rating!r} | krytycy={critics_rating!r}"
    )

    return MovieRow(title=title, user_rating=user_rating, critics_rating=critics_rating, url=url)


def scrape_filmweb_top_500(output_csv: str = "filmweb_top500.csv") -> None:
    driver = _setup_driver()
    wait = WebDriverWait(driver, 20)

    collected: list[MovieRow] = []
    seen_urls: set[str] = set()

    try:
        page = 1
        while len(collected) < 500:
            search_url = BASE_URL.format(page=page)
            print(f"[DEBUG] Wejście na stronę listy: {search_url} (zebrane: {len(collected)}/500)")
            driver.get(search_url)

            _try_close_didomi(driver, WebDriverWait(driver, 6))
            try:
                _wait_for_search_results_ready(driver, wait)
            except TimeoutException:
                print("[DEBUG] Timeout podczas oczekiwania na wyniki wyszukiwania.")
                _dump_debug_page(driver, f"search_timeout_page_{page}")
                raise

            page_links = _collect_movie_links_from_results(driver)
            if not page_links:
                print("[DEBUG] Brak linków na stronie — przerywam (możliwa zmiana layoutu / blokada).")
                _dump_debug_page(driver, f"search_no_links_page_{page}")
                break

            # Przechodzimy po linkach z tej strony
            for link in page_links:
                if len(collected) >= 500:
                    break
                if link in seen_urls:
                    continue

                seen_urls.add(link)
                try:
                    row = _scrape_movie_page(driver, wait, link)
                    collected.append(row)
                except TimeoutException:
                    print(f"[DEBUG] Timeout na stronie filmu: {link}")
                except Exception as e:
                    print(f"[DEBUG] Błąd podczas scrapowania filmu: {link} | {e!r}")

                _small_human_delay(0.35, 1.1)

            page += 1
            _small_human_delay(0.7, 1.6)

        # Zapis do CSV
        df = pd.DataFrame(
            [
                {
                    "Tytuł": r.title,
                    "Ocena użytkowników": r.user_rating,
                    "Ocena krytyków": r.critics_rating,
                    "URL": r.url,
                }
                for r in collected[:500]
            ]
        )
        df.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"[DEBUG] Zapisano {len(df)} rekordów do pliku: {output_csv}")

    finally:
        driver.quit()


if __name__ == "__main__":
    scrape_filmweb_top_500()
