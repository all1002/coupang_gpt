import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# 경로/환경 준비: 루트를 sys.path 에 추가 → .env 로드
# ──────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(dotenv_path=ROOT / ".env")

from coupang.partners import (  # noqa: E402
    CoupangPartnersClient,
    filter_and_score,
    LAST_DEBUG,
)

# ──────────────────────────────────────────────────────────────────────────────
# UI 설정
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Coupang 소싱 도구", layout="wide")
st.title("🔎 쿠팡 소싱 도구 (파트너스 API)")
st.caption("키워드 기반 상품 탐색 → 필터링/스코어링 → Deeplink 생성 → CSV 저장")

with st.sidebar:
    st.header("검색 설정")
    keyword = st.text_input("키워드", value="텀블러")
    limit = st.number_input("가져올 최대 상품 수(≤10)", min_value=1, max_value=10, value=5, step=1)
    sub_id = st.text_input("Sub ID (선택)", value=os.getenv("COUPANG_SUB_ID", ""))

    st.divider()
    st.header("필터 / 스코어링")
    min_price = st.number_input("최소 가격(원)", min_value=0, value=0, step=100)

    # ✅ 리뷰 수 범위: 숫자 입력 2개
    review_min = st.number_input("리뷰 수 최소", min_value=0, value=150, step=10)
    review_max = st.number_input("리뷰 수 최대", min_value=0, value=300, step=10)

    # 평점은 사용하지 않음(고정 0)
    min_rating = 0.0

    commission_rate = (
        st.number_input("예상 커미션 비율(%)", min_value=0.0, max_value=100.0, value=3.0, step=0.5) / 100.0
    )

    # 리뷰 수집 옵션(필요할 때만)
    enrich_reviews = st.checkbox(
        "🔎 리뷰 수 추가 수집(느림)", value=True,
        help="API 응답에 리뷰 수가 없을 때, 각 상품 상세 페이지를 읽어 '○○개 상품평'에서 리뷰수를 추출합니다."
    )

    st.divider()
    st.header("API 키 상태")
    st.write("ACCESS_KEY", "✅" if os.getenv("COUPANG_ACCESS_KEY") else "❌")
    st.write("SECRET_KEY", "✅" if os.getenv("COUPANG_SECRET_KEY") else "❌")

    # 기본은 OFF (필요 시에만 켜서 확인)
    show_debug = st.checkbox("디버그 보기(요청값/서명 재료)", value=False)

client = CoupangPartnersClient()

# ──────────────────────────────────────────────────────────────────────────────
# 검색 실행
# ──────────────────────────────────────────────────────────────────────────────
if st.button("검색 실행", type="primary"):
    try:
        raw = client.search_products(keyword, limit=limit, sub_id=sub_id or None)

        # ① 응답에서 리스트 꺼내기 (견고하게)
        candidates = None
        if isinstance(raw, dict):
            # {"rCode":"0","data":[...]} 형태
            if isinstance(raw.get("data"), list):
                candidates = raw["data"]
            # {"rCode":"0","data":{"products":[...]}} 형태
            elif isinstance(raw.get("data"), dict):
                for key in ("products", "productData", "items", "content"):
                    if isinstance(raw["data"].get(key), list):
                        candidates = raw["data"][key]
                        break
            # 그 외 변형
            if candidates is None:
                for key in ("products", "productData", "items", "content"):
                    if isinstance(raw.get(key), list):
                        candidates = raw[key]
                        break

        if not isinstance(candidates, list):
            candidates = []

        # ② 디버그(선택)
        if show_debug and candidates:
            st.info(f"API 원시 결과 개수:{len(candidates)}")
            st.json({"sample_keys": list(candidates[0].keys())})

        # ③ 응답에 리뷰/평점 키가 있는지 확인
        first = candidates[0] if candidates else {}
        has_rating = any(k in first for k in ("productRating", "rating", "ratingAverage", "ratingScore"))
        has_reviews = any(k in first for k in ("productReviewCount", "reviewCount", "ratingCount"))

        # 평점은 사용하지 않음
        min_rating = 0.0

        # 리뷰 수가 응답에 없고, 사용자가 범위를 지정했다면 → 추가 수집(옵션)
        effective_review_min, effective_review_max = review_min, review_max
        if not has_reviews:
            if enrich_reviews and candidates:
                candidates = client.enrich_review_counts(candidates, max_items=len(candidates))
                # enrich 후 재확인
                first = candidates[0] if candidates else {}
                has_reviews = any(k in first for k in ("productReviewCount", "reviewCount", "ratingCount"))
                if not has_reviews:
                    # 여전히 없으면 범위 해제
                    effective_review_min, effective_review_max = 0, 10**9
            else:
                effective_review_min, effective_review_max = 0, 10**9

        # ④ 스코어링/필터
        if not candidates:
            st.warning("원시 후보가 없습니다.")
        else:
            ranked = filter_and_score(
                candidates,
                min_price=min_price,
                min_rating=min_rating,                 # 항상 0.0
                review_min=effective_review_min,        # ✅ 범위 하한
                review_max=effective_review_max,        # ✅ 범위 상한
                commission_rate=commission_rate,
            )
            if not ranked:
                st.warning("조건에 맞는 결과가 없습니다.")
            else:
                # ── 결과 DataFrame 만들고 표준 컬럼 생성 ─────────────────────────
                df = pd.DataFrame(ranked)
                st.session_state["result_df"] = df

                # 가격 컬럼 통일
                if "_price" in df.columns:
                    df["price(원)"] = df["_price"].fillna(0).astype(float).round(0).astype(int)
                elif "productPrice" in df.columns:
                    df["price(원)"] = pd.to_numeric(df["productPrice"], errors="coerce").fillna(0)\
                        .astype(float).round(0).astype(int)
                else:
                    df["price(원)"] = 0

                # 리뷰수 컬럼 통일
                if "productReviewCount" in df.columns:
                    df["reviews(개)"] = pd.to_numeric(df["productReviewCount"], errors="coerce").fillna(0)\
                        .astype(float).round(0).astype(int)
                elif "_reviews" in df.columns:
                    df["reviews(개)"] = pd.to_numeric(df["_reviews"], errors="coerce").fillna(0)\
                        .astype(float).round(0).astype(int)
                else:
                    df["reviews(개)"] = 0

                # 보여줄 컬럼 구성(이미지를 앞쪽에)
                prefer_cols = [
                    "productImage",   # 이미지 썸네일
                    "productName",
                    "price(원)",
                    "reviews(개)",
                    "categoryName",
                    "productUrl",     # 링크
                ]
                show_cols = [c for c in prefer_cols if c in df.columns]
                display_df = df[show_cols].copy()

                st.success(f"{len(display_df)}개 결과")

                # 이미지/링크 렌더링 + 테이블 높이 키우기
                st.dataframe(
                    display_df,
                    use_container_width=True,
                    height=640,  # 표 높이
                    column_config={
                        "productImage": st.column_config.ImageColumn("이미지", width="medium"),
                        "productUrl": st.column_config.LinkColumn("바로가기", display_text="바로가기"),
                    },
                )

    except Exception as e:
        st.exception(e)

# ──────────────────────────────────────────────────────────────────────────────
# 후속: Deeplink 생성 & CSV 저장
# ──────────────────────────────────────────────────────────────────────────────
if "result_df" in st.session_state:
    df = st.session_state["result_df"]
    left, right = st.columns([1, 1])

    with left:
        sel = st.multiselect("deeplink 생성 대상 선택 (인덱스)", options=df.index.tolist())
        if st.button("선택 항목 Deeplink 생성"):
            try:
                urls = [
                    df.loc[i].get("productUrl") or df.loc[i].get("landingUrl")
                    for i in sel
                ]
                urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
                if not urls:
                    st.warning("URL 필드(productUrl/landingUrl)가 응답에 존재하는지 확인하세요.")
                else:
                    deeplink_resp = client.create_deeplinks(urls, sub_id=sub_id or None)
                    st.json(deeplink_resp)
            except Exception as e:
                st.exception(e)

    with right:
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV 다운로드", data=csv, file_name=f"coupang_sourcing_{keyword}.csv", mime="text/csv")

    if show_debug and LAST_DEBUG:
        st.subheader("🔎 디버그 정보 (최근 요청)")
        st.json(LAST_DEBUG)

# ──────────────────────────────────────────────────────────────────────────────
# 진단: Deeplink 단독 호출(선택)
# ──────────────────────────────────────────────────────────────────────────────
diagnose = st.sidebar.button("🔧 딥링크 진단 실행")
if diagnose:
    try:
        test = client.create_deeplinks(["https://www.coupang.com/np/search?q=tumbler"])
        st.success("딥링크 호출 성공 (키/서명 정상)")
        if show_debug:
            st.json(test)
    except Exception as e:
        st.error(f"딥링크 호출 실패: {e}")
    if show_debug and LAST_DEBUG:
        st.subheader("진단 디버그 정보")
        st.json(LAST_DEBUG)
