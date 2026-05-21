def fetch_chittorgarh() -> list[dict]:
    log.info("━━ SOURCE A: Chittorgarh ━━")
    results = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        r = scraper.get("https://www.chittorgarh.com/ipo/ipo_dashboard.asp", headers=CHROME_HEADERS, timeout=20)
        if r.status_code != 200:
            return results
        soup = BeautifulSoup(r.text, "lxml")
        # Find the main IPO table – it has class "table table-bordered table-condensed"
        table = soup.find("table", class_=re.compile(r"table"))
        if not table:
            return results
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Map expected columns
        col_map = {"name": -1, "open": -1, "close": -1, "price": -1, "lot": -1, "size": -1}
        for i, h in enumerate(headers):
            if "company" in h or "name" in h:
                col_map["name"] = i
            elif "open" in h:
                col_map["open"] = i
            elif "close" in h:
                col_map["close"] = i
            elif "price" in h or "issue price" in h:
                col_map["price"] = i
            elif "lot" in h:
                col_map["lot"] = i
            elif "size" in h or "amount" in h:
                col_map["size"] = i
        # If we didn't find a name column, fallback to first column
        if col_map["name"] == -1:
            col_map["name"] = 0

        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) <= col_map["name"]:
                continue
            entry = {"source": "Chittorgarh", "name": cols[col_map["name"]]}
            if col_map["open"] != -1 and col_map["open"] < len(cols):
                entry["open_date"] = cols[col_map["open"]]
            if col_map["close"] != -1 and col_map["close"] < len(cols):
                entry["close_date"] = cols[col_map["close"]]
            if col_map["price"] != -1 and col_map["price"] < len(cols):
                entry["issue_price"] = cols[col_map["price"]]
            if col_map["lot"] != -1 and col_map["lot"] < len(cols):
                entry["lot_size"] = cols[col_map["lot"]]
            if col_map["size"] != -1 and col_map["size"] < len(cols):
                entry["issue_size"] = cols[col_map["size"]]
            results.append(entry)
        log.info(f"  ✓ Chittorgarh: {len(results)} IPOs")
    except Exception as e:
        log.warning(f"  Chittorgarh error: {e}")
    return results
