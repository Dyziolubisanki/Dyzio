from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time

def scrape_filmweb_smart():
    options = webdriver.ChromeOptions()
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--window-size=1920,1080') # Wymuszamy duży ekran, by uniknąć wersji mobilnej
    
    driver = webdriver.Chrome(options=options)
    
    url = "https://www.filmweb.pl/search#/film?page=1"
    print(f"Ładuję stronę: {url}")
    driver.get(url)
    
    # 1. SPRYTNE CZEKANIE: Zamiast zgadywać klasy HTML, czekamy na jakikolwiek link do filmu
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href, '/film/')]"))
        )
        print("Wyniki załadowały się pomyślnie!")
    except Exception as e:
        print("\nBŁĄD: Strona nie załadowała się w porę.")
        print("Zapisuję zrzut ekranu do pliku 'co_widzi_bot.png', żebyśmy mogli sprawdzić co blokuje dostęp...")
        driver.save_screenshot("co_widzi_bot.png")
        driver.quit()
        return

    # 2. PRÓBA ZAMKNIĘCIA CIASTECZEK (RODO)
    try:
        # Filmweb często używa systemu "Didomi" do ciasteczek
        cookie_btn = driver.find_element(By.ID, "didomi-notice-agree-button")
        cookie_btn.click()
        print("Zaakceptowano ciasteczka.")
        time.sleep(1)
    except:
        print("Nie znaleziono okna ciasteczek (lub zostało już zignorowane).")

    # 3. POBRANIE LINKÓW DO FILMÓW
    # Szukamy wszystkich tagów <a>, które mają w adresie "/film/"
    elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/film/')]")
    
    # Używamy zbioru (set), aby odfiltrować duplikaty (często obrazek i tytuł mają ten sam link)
    movie_links = set()
    for el in elements:
        href = el.get_attribute("href")
        if href:
            movie_links.add(href)
            
    # Zamieniamy z powrotem na listę
    movie_links = list(movie_links)
    
    print(f"Znaleziono {len(movie_links)} unikalnych linków do filmów na tej stronie.")
    
    # Dla testu wypiszmy 5 pierwszych linków, by upewnić się, że to działa
    print("\nPierwsze 5 znalezionych filmów:")
    for link in movie_links[:5]:
        print(link)

    driver.quit()

# Uruchamiamy
scrape_filmweb_smart()