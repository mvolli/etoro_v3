import urllib.request, urllib.parse, re, html, time, random

UA_LIST = [
 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
 "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

def _get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": random.choice(UA_LIST), "Accept-Language":"en-US,en;q=0.9", "Accept":"text/html"}, **(headers or {}))
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

def _parse_blocks(data):
    results = []
    blocks = re.split(r'class="result results_links', data)
    for b in blocks[1:]:
        ma = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        ms = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        if not ma: continue
        href, title = ma.group(1), ma.group(2)
        real = href
        mm = re.search(r'uddg=([^&]+)', href)
        if mm: real = urllib.parse.unquote(mm.group(1))
        title = html.unescape(re.sub(r'<[^>]+>', '', title)).strip()
        snippet = ""
        if ms:
            snippet = html.unescape(re.sub(r'<[^>]+>', '', ms.group(1))).strip()
            snippet = re.sub(r'\s+', ' ', snippet)
        results.append({"title": title, "url": real, "snippet": snippet})
    return results

def ddg_search(q, max_results=8, attempts=5):
    last = None
    for a in range(attempts):
        try:
            url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
            data = _get(url, timeout=25)
            res = _parse_blocks(data)[:max_results]
            if res:
                return {"query": q, "results": res}
            last = "empty"
        except Exception as e:
            last = str(e)
        time.sleep(1.5 + a*2.5 + random.random()*1.5)
    return {"query": q, "results": [], "error": last}

def fetch_text(url, char_limit=14000, attempts=3):
    last = None
    for a in range(attempts):
        try:
            raw = _get(url, timeout=30)
            raw2 = re.sub(r'(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>', ' ', raw)
            raw2 = re.sub(r'(?is)<(nav|footer|header|aside)[^>]*>.*?</\1>', ' ', raw2)
            m = re.search(r'(?is)<(main|article)[^>]*>', raw2)
            if m: raw2 = raw2[m.start():]
            text = re.sub(r'(?is)<br\s*/?>', '\n', raw2)
            text = re.sub(r'(?is)</(p|div|li|h1|h2|h3|h4|tr|section|table|td)>', '\n', text)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = html.unescape(text)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n\s*\n+', '\n', text)
            text = re.sub(r' +', ' ', text)
            if len(text.strip()) > 120:
                return {"url": url, "text": text[:char_limit], "total_len": len(text)}
            last = "short"
        except Exception as e:
            last = str(e)
        time.sleep(1.5 + a*2.0)
    return {"url": url, "error": last}
