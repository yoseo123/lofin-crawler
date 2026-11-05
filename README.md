# Lofin365 정책 크롤러 (정책 수집 -> 상세 추출 -> Markdown/XLSX + 검색 색인 저장)

## 1) 설치
```bash
cd lofin-crawler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 1-1) `.env` 설정
웹 UI 세션 키 등 선택 환경변수는 프로젝트 루트의 `.env`에 둘 수 있습니다.
`crawl_lofin.py`, `webapp.py` 실행 시 `.env`가 자동 로드됩니다.

## 2) CLI 실행 예시 (인구 정책)
```bash
python crawl_lofin.py --keyword 인구 --limit 50 --output population_policies.md --headed --slow-mo 200
```

검색 앱에서 사용하려면 같은 이름의 `.json` 색인을 함께 사용하세요.
아래 명령은 `population_policies_YYYYMMDD_HHMMSS.md`와 `population_policies_YYYYMMDD_HHMMSS.json`을 함께 생성합니다.

```bash
python crawl_lofin.py --keyword 인구 --limit 50 --output population_policies.md
```

XLSX도 필요하면 `--format xlsx` 또는 `--format both`를 사용합니다.

```bash
python crawl_lofin.py --keyword 인구 --limit 50 --format both --output population_policies.md
```

## 2-1) 연도/지역 필터와 함께 실행
```bash
python crawl_lofin.py --keyword 인구 --year 2026-01-01 --region 서울 --limit 50 --output population_policies.md --headed
```

검색어를 비우면 연도/지역 조건 기준으로 전체 조회를 시도합니다.
CLI/웹 UI 입력 상한은 최대 500,000건입니다. 대량 수집은 대상 사이트 응답속도와 상세 페이지 수집 시간에 따라 오래 걸릴 수 있습니다.
`--limit`이 `--chunk-size`보다 크면 기본적으로 10,000건 단위 part 파일과 manifest를 생성합니다.

```bash
python crawl_lofin.py --year 2026-01-01 --limit 500000 --chunk-size 10000 --output "2026 전국 정책.md"
```

## 2-2) 대량(6천 건+) 수집 권장 옵션
상세 페이지는 기본적으로 `popup API 우선 + HTTP 상세 페이지 fallback`을 병렬로 호출합니다.

```bash
python crawl_lofin.py \
  --keyword 인구 \
  --limit 6000 \
  --detail-request-timeout-ms 7000 \
  --detail-concurrency 24 \
  --detail-batch-size 1000 \
  --detail-runtime-timeout-ms 800 \
  --detail-delay-ms 20 \
  --output population_policies_6000.md
```

- `--detail-concurrency`: 상세 HTTP 요청 병렬 처리 수. 기본값은 24입니다. 높일수록 빠르지만 대상 서버 오류율과 부하가 증가할 수 있습니다.
- `--detail-batch-size`: 상세 병렬 수집 전에 모을 목록 건수. 기본값은 1000입니다. 크게 잡을수록 페이지 이동 대기 비중이 줄지만 진행률 갱신 간격이 길어집니다.
- `--detail-delay-ms`: 건별 지연(ms). 낮출수록 빠르지만 대상 서버 부하가 증가합니다.
- `--detail-request-timeout-ms`: 상세 HTTP 요청 타임아웃(ms).
- `--detail-runtime-timeout-ms`: 목록 구조를 읽지 못해 브라우저 클릭 fallback을 사용할 때의 런타임 대기(ms).
- `--chunk-size`: part 파일당 저장 건수. `0`이면 기존처럼 단일 파일로 저장합니다.

## 2-3) 중앙정부(열린재정) 목록 수집
중앙정부 `재정사업별 설명자료` 목록을 수집하려면 `--source central`을 사용합니다.
`세부사업명` 상세팝업에서 다운로드되는 HWPX를 읽어 `사업내용` 컬럼을 가능한 경우 자동 보강합니다.
예산안/예산총괄표는 현재 제외합니다.

```bash
python crawl_lofin.py \
  --source central \
  --year 2026 \
  --limit 200 \
  --output central_2026.md
```

- `--year`는 `2026` 또는 `2026-01-01` 모두 사용 가능합니다.
- 중앙정부 회계년은 `2018~2026` 범위만 지원합니다.
- `--keyword`를 비우면 해당 회계년 전체 조회를 시도합니다.

## 3) HTML 웹에서 실행
```bash
python webapp.py
```
- 브라우저에서 `http://127.0.0.1:8787` 접속
- 폼에서 `검색어/출처/날짜(또는 회계년)/지역/갯수/청크 크기/파일 형식` 입력 후 실행
- 검색어를 비우면 선택한 조건 기준으로 전체 조회를 시도합니다.
- `출처=중앙정부` 선택 시 `회계년(2018~2026)`만 표시되고 `지역` 입력은 숨김 처리됩니다.
- 웹 결과 파일은 프로젝트의 전용 `outputs/` 폴더에 `*_YYYYMMDD_HHMMSS.md`와 `*_YYYYMMDD_HHMMSS.json`으로 생성
- `파일 형식=Markdown + XLSX`를 고르면 `.md`, `.xlsx`, `.json`을 함께 생성
- 대량 수집 시 `*_part001.md`, `*_part001.json`, `*_manifest.json` 형식으로 나뉘어 생성

### 웹 접근과 결과 파일 보안

- 기본 바인딩은 `127.0.0.1`이며 같은 컴퓨터에서만 사용합니다. 기본 로컬 모드는 외부 IP, 임의 Host 및 전달 헤더를 거부합니다. CSRF 검증은 로그인과 별개로 유지됩니다.
- 원격 접속이 필요하면 `LOFIN_ALLOW_REMOTE=true`, `LOFIN_WEB_HOST=0.0.0.0`, **32자 이상의 무작위 `LOFIN_WEB_PASSWORD`**를 설정합니다. 브라우저의 HTTP Basic 로그인 사용자명은 `lofin`입니다. 비밀번호가 없거나 짧으면 원격 모드는 동작하지 않습니다. 비밀번호는 URL·명령줄 인수·저장소에 넣지 말고 권한을 제한한 `.env` 또는 비밀 환경변수로 전달하세요.
- 원격 모드에서는 localhost에서 오는 프록시 요청에도 인증을 요구합니다. `X-Forwarded-For`는 접근 허용이나 요청 제한의 사용자 구분에 사용하지 않습니다. 프록시 뒤에서는 같은 프록시를 통한 실행 요청 제한이 공유됩니다.
- **외부에 Flask 개발 서버나 HTTP Basic을 평문 HTTP로 공개하지 마세요.** 신뢰할 수 있는 HTTPS 역방향 프록시 또는 VPN/SSH 터널 뒤에서만 사용하고, HTTPS에서는 `LOFIN_WEB_COOKIE_SECURE=true`와 `LOFIN_ALLOWED_ORIGINS=https://crawler.example.com`처럼 정확한 외부 origin을 설정하세요. 프록시는 Authorization 헤더를 전달해야 합니다. 이는 하나의 공유 비밀번호이며 사용자별 권한 분리 기능은 아닙니다.
- Docker도 명시적인 원격 모드와 비밀번호가 필요합니다. 컨테이너에는 위 환경변수를 전달하되 호스트 포트는 `-p 127.0.0.1:8787:8787`로 제한하고, `/app/outputs`를 별도 볼륨으로 보존하세요. 외부 접근은 HTTPS 프록시에서만 허용하세요. `EXPOSE`만으로 포트가 게시되지는 않습니다.
- 웹 다운로드와 최근 파일 목록은 `outputs/` 안의 일반 MD/XLSX/JSON 파일만 제공합니다. 소스 루트의 README, SQLite 원본 기록과 sidecar, JSONL, 숨김 임시 파일, 심볼릭 링크는 제공하지 않습니다. 웹 결과는 재시작 후에도 목록에 표시됩니다. 기존 루트 결과를 계속 내려받으려면 필요한 **내보내기 파일만** 관리자가 `outputs/`로 복사하세요. 복구용 SQLite는 웹 다운로드로 이동할 필요가 없습니다.
- `outputs/`, JSONL, `*_resume.sqlite3`와 journal/WAL/SHM·잠금 파일, 숨김 atomic 임시 파일은 Git과 Docker 컨텍스트에서 제외합니다. 일반 `poetry.lock`, 코드, SQLite 테스트 fixture는 새 규칙으로 숨기지 않습니다. 기존에 추적된 파일이나 이전 커밋/이미지에 든 데이터는 ignore만으로 제거되지 않습니다. CLI로 임의 위치에 저장한 일반 데이터베이스는 자동 제외하지 않으므로 별도 데이터 폴더를 사용하세요.
- PR #2의 중단 복구 기능과 독립적인 변경입니다. 두 변경을 함께 적용하면 웹 청크 출력과 SQLite 복구 기록이 모두 `outputs/`에 저장되며, CLI 경로와 재개 계약은 그대로입니다. 이 설정은 실제 배포/NAS를 변경하거나 공개 여부를 검사하지 않습니다.

- 기존 `POST /`도 `POST /api/run`과 같은 단일 작업 큐와 실행 요청 제한을 사용합니다. JavaScript UI는 그대로이며, JavaScript 없이 폼을 제출하는 클라이언트는 동기 HTML 결과 대신 작업 ID가 포함된 JSON(202)을 받습니다. `GET /api/jobs/<id>`로 상태를 조회하세요.

검증: `python -m unittest discover -s tests -v`. 실제 수집 사이트 호출 없이 테스트합니다.

### 3-1) Windows 더블클릭 실행
- `start_webapp.bat` 더블클릭
- 자동으로 서버 실행 + 브라우저(`http://127.0.0.1:8787`) 오픈

## 4) 결과 컬럼
- `번호`, `사업명`, `지역`, `자치단체`, `회계연도`, `조직`, `사업목적`, `사업기간`, `총사업비`, `사업내용`, `지원형태`, `지원조건`, `사업위치`, `시행주체`, `추진근거`, `추진경위`, `추진계획`

중앙정부(`--source central`) 결과 컬럼:
- `번호`, `회계연도`, `소관명`, `회계명`, `분야명`, `부문명`, `프로그램명`, `단위사업명`, `세부사업명`, `예산액`, `사업내용(가능 시)`

## 5) 셀렉터 조정이 필요한 경우
사이트 구조 변경 시 `crawl_lofin.py` 상단의 아래 목록을 수정:
- `YEAR_FILTER_SELECTORS`
- `REGION_FILTER_SELECTORS`
- `SEARCH_INPUT_SELECTORS`
- `SEARCH_BUTTON_SELECTORS`
- `LIST_ITEM_SELECTORS`
- `DETAIL_PANEL_SELECTORS`
- `DETAIL_CLOSE_SELECTORS`

## 6) 테스트
```bash
python3 -m unittest discover -s tests -v
```

## 7) 유사 정책 검색용 RAG 색인
수집 결과의 `*_part*.json` 색인을 OpenAI Embeddings + Qdrant 검색 색인으로 변환합니다.
RAG 기능을 쓰려면 실행 환경에서 `OPENAI_API_KEY`를 직접 설정하세요.

```bash
python build_policy_rag_index.py prepare-batch \
  --input-glob "2026 전국 정책_*_part*.json" \
  --output-jsonl policy_embedding_batch.jsonl

python build_policy_rag_index.py submit-batch --input-jsonl policy_embedding_batch.jsonl
python build_policy_rag_index.py check-batch <batch_id>
python build_policy_rag_index.py download-batch-output <output_file_id> --output-jsonl policy_embedding_batch_output.jsonl

python build_policy_rag_index.py upsert-batch \
  --input-glob "2026 전국 정책_*_part*.json" \
  --batch-output-jsonl policy_embedding_batch_output.jsonl
```

- `prepare-batch`는 기본적으로 Batch 요청을 파일당 최대 50,000건으로 자동 분할합니다.
- 분할된 경우 `policy_embedding_batch_manifest.json`의 `batch_files`에 나온 각 JSONL을 `submit-batch`로 각각 제출하고, 완료된 출력 JSONL을 각각 `upsert-batch` 하면 됩니다.
- Qdrant 기본 주소: `http://127.0.0.1:6333`
- Qdrant 기본 collection: `policy_cases_v1`
- OpenAI embedding 기본 모델: `text-embedding-3-large`, `dimensions=1536`
- 웹 API: `POST /api/similar-policies` (`query`, `top_k`, `year_from`, `year_to`, `region`, `municipality`, `category`). 웹앱의 CSRF 보호가 적용되므로 브라우저/API 클라이언트에서는 기존 CSRF 토큰을 `X-CSRF-Token` 헤더로 함께 전송합니다.

