# coupang/partners.py
# 쿠팡 파트너스 API 클라이언트 + 필터/스코어링 + 리뷰수 보강(enrich) 모듈

import os
import re
import time
import hmac
import hashlib
import sys
import asyncio
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

API_HOST = "https://api-gateway.coupang.com"
SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"

# Streamlit에서 확인할 수 있도록 최근 요청 진단정보 저장
LAST_DEBUG: Dict[str, Any] = {}


# ──────────────────────────────────────────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────────────────────────────────────────
def _utc_signed_datetime() -> str:
    """서버 사양에 맞춘 2자리 연도 포맷(예: 251018T120517Z)."""
    return time.strftime("%y%m%dT%H%M%SZ", time.gmtime())


def _canonical_query(params: Dict[str, Any]) -> str:
    """쿠팡 API canonical query 생성."""
    if not params:
        return ""
    parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        parts.append(
            f"{urllib.parse.quote(str(k), safe='-_.~')}="
            f"{urllib.parse.quote(str(v), safe='-_.~')}"
        )
    return "&".join(parts)


def _build_auth(method: str, path: str, query: str, access_key: str, secret_key: str):
    """
    Authorization 헤더 생성.
    message = signed_date + METHOD + path + query (물음표 없이 query 바로 이어붙임)
    ※ 쉼표 뒤 공백 없는 포맷(중요)
    """
    signed_date = _utc_signed_datetime()
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    auth = (
        f"CEA algorithm=HmacSHA256,access-key={access_key},"
        f"signed-date={signed_date},signature={signature}"
    )
    return auth, signed_date


# ──────────────────────────────────────────────────────────────────────────────
# 스코어링/필터 유틸
# ──────────────────────────────────────────────────────────────────────────────
def _to_number(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x)
    for tok in ["원", ",", " ", "\u00a0"]:
        s = s.replace(tok, "")
    try:
        return float(s)
    except Exception:
        return 0.0


def _pick(d: Dict, keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def filter_and_score(
    items: List[Dict],
    min_price: int = 0,
    min_rating: float = 0.0,           # 평점은 사용하지 않더라도 값이 있을 때만 필터
    review_min: int = 0,
    review_max: Optional[int] = None,
    commission_rate: float = 0.03,
) -> List[Dict]:
    """
    원시 상품 리스트를 간단한 규칙으로 필터링/스코어링하여 정렬된 리스트 반환.
    - 가격/평점/리뷰 구간: '값이 존재할 때'만 필터 동작
    - 리뷰/평점이 응답에 없으면 해당 필터는 건너뜀
    """
    ranked: List[Dict] = []

    for it in items:
        price_raw   = _pick(it, ["productPrice", "salePrice", "price", "priceSales", "lowPrice"], None)
        rating_raw  = _pick(it, ["productRating", "rating", "ratingAverage", "ratingScore"], None)
        reviews_raw = _pick(it, ["productReviewCount", "reviewCount", "ratingCount"], None)

        price   = _to_number(price_raw)
        rating  = _to_number(rating_raw)
        reviews = _to_number(reviews_raw)

        # 가격 필터
        if price < float(min_price):
            continue

        # 평점 필터(값 있을 때만)
        if rating_raw is not None and rating < float(min_rating):
            continue

        # 리뷰 구간 필터(값 있을 때만)
        if reviews_raw is not None:
            if reviews < float(review_min):
                continue
            if review_max is not None and reviews > float(review_max):
                continue

        # 간이 스코어: 커미션 × (리뷰 보정) × (평점 보정) × (rank 보정)
        commission = float(price) * float(commission_rate)
        rank_raw = _pick(it, ["rank"], None)
        rank_val = _to_number(rank_raw)
        rank_boost = 1.0 + (max(0.0, 10.0 - rank_val) * 0.02) if rank_raw is not None else 1.0
        score = commission * max(1.0, (reviews + 1.0) ** 0.3) * max(1.0, rating / 5.0) * rank_boost

        row = dict(it)
        row["_price"] = round(price, 2)
        row["_rating"] = round(rating, 2) if rating_raw is not None else None
        row["_reviews"] = int(reviews) if reviews_raw is not None else None
        row["_est_commission"] = round(commission, 2)
        row["_score"] = round(score, 4)
        ranked.append(row)

    ranked.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return ranked


# ──────────────────────────────────────────────────────────────────────────────
# API 클라이언트
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class CoupangPartnersClient:
    access_key: str = ""
    secret_key: str = ""
    sub_id: Optional[str] = None
    timeout: int = 20

    def __post_init__(self):
        if not self.access_key:
            self.access_key = (os.getenv("COUPANG_ACCESS_KEY", "") or "").strip()
        if not self.secret_key:
            self.secret_key = (os.getenv("COUPANG_SECRET_KEY", "") or "").strip()
        if isinstance(self.sub_id, str):
            self.sub_id = self.sub_id.strip() or None
        if not self.access_key or not self.secret_key:
            raise ValueError("COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 비어있음 (.env 로딩 확인)")

    # ── 내부: 공통 요청 ─────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        method = method.upper()
        params = params or {}
        query = _canonical_query(params)

        url = f"{API_HOST}{path}" + (f"?{query}" if query else "")
        auth, signed_date = _build_auth(method, path, query, self.access_key, self.secret_key)

        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Authorization": auth,  # 공백 없는 포맷(중요)
        }

        # 진단 정보 저장
        LAST_DEBUG.clear()
        LAST_DEBUG.update({
            "final_url": url,
            "message_preview": f"{signed_date}{method}{path}{query}"[:160] + "...",
            "Authorization": auth[:120] + "...",
        })

        resp = requests.request(method, url, headers=headers, json=json_body, timeout=self.timeout)
        LAST_DEBUG["status_code"] = resp.status_code
        try:
            LAST_DEBUG["error_body"] = resp.text[:800] if resp.status_code >= 400 else ""
        except Exception:
            pass

        resp.raise_for_status()
        return resp.json()

    # ── 공개 API ────────────────────────────────────────────────────────
    def search_products(self, keyword: str, limit: int = 10, sub_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"keyword": keyword, "limit": max(1, min(int(limit), 10))}
        if sub_id or self.sub_id:
            params["subId"] = sub_id or self.sub_id
        return self._request("GET", SEARCH_PATH, params=params)

    def create_deeplinks(self, coupang_urls: List[str], sub_id: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"coupangUrls": coupang_urls}
        if sub_id or self.sub_id:
            s = sub_id or self.sub_id
            # 일부 환경 호환을 위해 소문자/대문자 키 모두 포함
            body["subId"] = s
            body["SubId"] = s
        return self._request("POST", DEEPLINK_PATH, json_body=body)

    # ──────────────────────────────────────────────────────────────────────
    # (옵션) 상세 페이지에서 리뷰 수 추가 수집
    # ──────────────────────────────────────────────────────────────────────
    def enrich_review_counts(self, items: List[Dict], max_items: int = 20, timeout: int = 10) -> List[Dict]:
        """
        리뷰 수가 API 응답에 없을 때, 상품 상세에서 값을 추출해 productReviewCount를 채운다.
        1) requests 로 정식 상품 URL(https://www.coupang.com/vp/products/{productId}) 우선 접근
        2) 실패하면 Playwright(설치된 경우)로 렌더링 후 텍스트/JSON에서 추출
        - 다양한 표현(텍스트/JSON) 패턴을 모두 탐색
        - UA 로테이션 + 랜덤 지연 + 재시도로 성공률 향상
        - 실패 시 None으로 표기 (UI에서 0으로 보정 표시 가능)
        """
        import random
        from time import sleep

        # ── 공통 준비 ────────────────────────────────────────────────────
        ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        ]

        text_json_patterns = [
            re.compile(r'([\d,]+)\s*개\s*(?:상품평|리뷰)'),
            re.compile(r'(?:상품평|리뷰)\s*([\d,]+)\s*개'),
            re.compile(r'aria-label="\s*상품평\s*([\d,]+)\s*개"'),
            re.compile(r'"reviewCount"\s*:\s*([0-9]+)'),
            re.compile(r'"totalReviewCount"\s*:\s*([0-9]+)'),
            re.compile(r'"reviewTotalCount"\s*:\s*([0-9]+)'),
        ]

        def extract_count_from_html(html: str) -> Optional[int]:
            if not html:
                return None
            for p in text_json_patterns:
                m = p.search(html)
                if m:
                    try:
                        return int(m.group(1).replace(",", ""))
                    except Exception:
                        pass
            return None

        def candidates_for_item(it: Dict) -> List[str]:
            urls: List[str] = []
            pid = str(it.get("productId") or "").strip()
            if pid.isdigit():
                urls.append(f"https://www.coupang.com/vp/products/{pid}")
            for key in ("productUrl", "landingUrl"):
                u = it.get(key)
                if isinstance(u, str) and u.startswith("http"):
                    urls.append(u)
            # 중복 제거
            seen, uniq = set(), []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    uniq.append(u)
            return uniq

        # ── 1단계: requests로 시도 ─────────────────────────────────────
        req_sess = requests.Session()
        req_sess.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        # ── 2단계: Playwright 준비 (없으면 None) ───────────────────────
        try:
            from playwright.sync_api import sync_playwright
            playwright_available = True
        except Exception:
            playwright_available = False

        browser_ctx = None
        page = None

        def ensure_browser():
            """Playwright 브라우저/컨텍스트/페이지 1회만 띄워 재사용.
            - Windows에서 asyncio 이벤트 루프 정책을 Proactor로 강제
            - playwright 시작/launch 실패 시 안전하게 None을 반환(=requests만 사용)
            """
            nonlocal browser_ctx, page
            if not playwright_available:
                return None
            if browser_ctx and page:
                return page
        
            # ✅ Windows에서 서브프로세스 사용 가능하도록 이벤트 루프 정책 지정
            try:
                if sys.platform.startswith("win"):
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass
            
            # ✅ playwright 시작 (실패하면 폴백)
            try:
                _p = sync_playwright().start()
            except Exception as e:
                LAST_DEBUG["playwright_error"] = f"start failed: {type(e).__name__}: {e}"
                return None
        
            # ✅ chromium launch (설치 안되어 있거나 권한 문제면 폴백)
            try:
                browser = _p.chromium.launch(headless=True)
            except Exception as e:
                LAST_DEBUG["playwright_error"] = f"launch failed: {type(e).__name__}: {e}"
                try:
                    _p.stop()
                except Exception:
                    pass
                return None
        
            try:
                context = browser.new_context(
                    user_agent=random.choice(ua_pool),
                    viewport={"width": 1280, "height": 900},
                    java_script_enabled=True,
                )
                pg = context.new_page()
                # 종료 핸들 저장
                browser._p = _p
                browser._context = context
                browser._page = pg
                browser_ctx = browser
                page = pg
                return page
            except Exception as e:
                LAST_DEBUG["playwright_error"] = f"context/new_page failed: {type(e).__name__}: {e}"
                try:
                    browser.close()
                    _p.stop()
                except Exception:
                    pass
                return None

        def close_browser():
            nonlocal browser_ctx, page
            try:
                if browser_ctx:
                    browser_ctx._context.close()
                    browser_ctx.close()
                    browser_ctx._p.stop()
            except Exception:
                pass
            browser_ctx = None
            page = None

        updated = 0
        try:
            for it in items:
                if updated >= max_items:
                    break
                if it.get("productReviewCount") is not None:
                    continue

                urls = candidates_for_item(it)
                count_val: Optional[int] = None

                # 1) requests로 1~3회 재시도
                for url in urls:
                    for _ in range(3):
                        try:
                            req_sess.headers["User-Agent"] = random.choice(ua_pool)
                            sleep(random.uniform(0.15, 0.5))
                            r = req_sess.get(url, timeout=timeout, allow_redirects=True)
                            if r.status_code >= 400:
                                continue
                            html = r.text or ""
                            count_val = extract_count_from_html(html)
                            if count_val is None and r.url != url:
                                r2 = req_sess.get(r.url, timeout=timeout, allow_redirects=True)
                                if r2.status_code < 400:
                                    count_val = extract_count_from_html(r2.text or "")
                            if count_val is not None:
                                break
                        except Exception:
                            continue
                    if count_val is not None:
                        break

                # 2) Playwright 폴백
                if count_val is None and playwright_available:
                    pg = ensure_browser()
                    if pg is not None:
                        for url in urls:
                            try:
                                pg.context.set_extra_http_headers({"User-Agent": random.choice(ua_pool)})
                                sleep(random.uniform(0.15, 0.4))
                                pg.goto(url, wait_until="load", timeout=timeout * 1000)
                                try:
                                    pg.wait_for_load_state("networkidle", timeout=3000)
                                except Exception:
                                    pass

                                # DOM 텍스트 전체에서 탐색
                                txt = pg.inner_text("body")
                                count_val = extract_count_from_html(txt)

                                if count_val is None:
                                    inner = pg.evaluate("() => document.body.innerText || ''")
                                    count_val = extract_count_from_html(inner)

                                if count_val is not None:
                                    break
                            except Exception:
                                continue

                it["productReviewCount"] = count_val  # 실패 시 None 기록(표시단에서 0으로 보정 가능)
                updated += 1

        finally:
            close_browser()

        return items
