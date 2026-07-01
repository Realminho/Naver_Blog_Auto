# 핸드폰버전 - Streamlit Cloud 배포용

이 폴더는 핸드폰에서 PC 없이 단독으로 사용하기 위한 버전입니다.
정확히는 핸드폰 안에서 Python을 직접 실행하는 것이 아니라, Streamlit Cloud에 프로그램을 올려두고 핸드폰 브라우저나 APK WebView로 접속하는 방식입니다.

핸드폰 버전에서는 다음이 가능합니다.

- 입력칸을 전부 채우지 않아도 AI 글 생성
- 사진/동영상 업로드
- 네이버 블로그처럼 보이는 미리보기
- 제목/본문 수정
- 제목/본문 복사
- 네이버 글쓰기 화면 열기

중요: 핸드폰 단독/Streamlit Cloud 버전은 네이버 편집기 안에 자동으로 글을 주입하는 기능은 넣지 않았습니다. 보안상 서버가 내 핸드폰의 네이버 로그인 화면과 편집기를 직접 조작할 수 없기 때문입니다. PC버전은 Chrome 자동입력을 지원합니다.

---

## 전체 흐름

```txt
이 폴더를 GitHub에 업로드
→ Streamlit Cloud에서 앱 만들기
→ API 키를 Secrets에 입력
→ 생성된 주소를 핸드폰에서 접속
→ 홈 화면에 추가하면 앱처럼 사용 가능
```

---

## 1. GitHub에 올릴 파일

이 폴더 안의 파일을 GitHub 저장소에 올리세요.

필수 파일:

```txt
app.py
requirements.txt
.streamlit/secrets.toml.example
```

`secrets.toml.example`은 예시 파일입니다. 실제 API 키를 GitHub에 올리지 마세요.

---

## 2. Streamlit Cloud에서 설정

Streamlit Cloud에서 새 앱을 만들고 GitHub 저장소를 연결합니다.

앱 실행 파일은 아래처럼 선택합니다.

```txt
app.py
```

---

## 3. API 키 넣는 위치

Streamlit Cloud의 App settings 또는 Secrets 메뉴에 아래 내용을 넣습니다.

```toml
NVIDIA_API_KEY = "YOUR_API_KEY"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "minimaxai/minimax-m3"
```

`YOUR_API_KEY`만 실제 NVIDIA API 키로 바꾸세요.

참고: `https://integrate.api.nvidia.com/v1`는 보통 모델 선택 페이지 주소라서, 프로그램은 실제 API 호출 시 자동으로 OpenAI 호환 API 주소로 보정합니다.

---

## 4. 핸드폰에서 접속

배포가 끝나면 아래처럼 주소가 생깁니다.

```txt
https://내앱이름.streamlit.app
```

핸드폰 Chrome 또는 Safari에서 이 주소로 접속하면 됩니다.

---

## 5. 핸드폰에서 작성하는 순서

1. 앱 주소 접속
2. 아는 정보만 입력
3. 사진/동영상 업로드
4. `생성하기` 클릭
5. `네이버식 미리보기`에서 확인
6. `수정/작성하기`에서 제목/본문 수정
7. `제목 복사` 클릭 후 네이버 글쓰기 제목칸에 붙여넣기
8. `본문 복사` 클릭 후 네이버 글쓰기 본문칸에 붙여넣기
9. 사진/동영상은 네이버 앱 또는 모바일 웹 편집기에서 직접 업로드
10. 최종 확인 후 발행

---

## 6. 홈 화면에 앱처럼 추가

### Android Chrome

1. 앱 주소 접속
2. 오른쪽 위 점 3개 메뉴
3. `홈 화면에 추가`
4. 이름 입력 후 추가

### iPhone Safari

1. 앱 주소 접속
2. 공유 버튼
3. `홈 화면에 추가`
4. 이름 입력 후 추가

---

## 7. APK까지 만들고 싶을 때

먼저 이 Streamlit Cloud 버전을 배포해서 주소를 만든 뒤, `핸드폰버전_APK_WebView` 폴더의 Android 프로젝트에 그 주소를 넣으면 됩니다.


---

## 새로고침해도 입력값 유지

이번 버전은 자동 임시저장 기능이 들어 있습니다.
제품명, 주소, 특징, 가격, 말투 같은 입력칸과 최근 생성 결과가 새로고침 후에도 최대한 복원됩니다.

단, 핸드폰 브라우저에서 업로드한 사진/동영상 파일은 보안 정책상 새로고침 후 다시 선택해야 할 수 있습니다.


## 생성하기 후 미리보기가 안 보일 때

이번 버전은 NVIDIA API 오류가 나도 템플릿 초안을 자동으로 만들어 미리보기를 보여줍니다.
Streamlit Cloud의 Secrets에는 아래처럼 넣으세요.

```toml
NVIDIA_API_KEY = "nvapi-새로발급받은키"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "minimaxai/minimax-m3"
```

`Bearer`는 넣지 않습니다.


## NVIDIA API 타임아웃이 날 때

`Read timed out` 오류는 API 키 오류가 아니라 NVIDIA 서버 응답이 오래 걸려서 끊긴 상태입니다.
이번 버전은 기본 `NVIDIA_MAX_TOKENS=2500`, `NVIDIA_TIMEOUT_SECONDS=240`으로 조정했습니다.
그래도 느리면 왼쪽 설정에서 **AI 응답 길이 제한**을 1500~2500으로 낮춘 뒤 다시 생성하세요.

## 참고 링크 입력 기능

핸드폰 버전에서도 제품/장소 정보 링크와 다른 블로그 리뷰 링크를 여러 개 넣을 수 있습니다. `생성하기`를 누르면 공개 페이지에서 제목, 설명, 본문 일부를 읽어 AI 글 생성에 참고합니다.

다른 리뷰어의 글은 그대로 복사하지 않고, 분위기·장단점·방문 팁을 참고해서 새로운 문장으로 재구성하도록 되어 있습니다.


---

## 쿠팡 제품 링크 보완

이번 버전은 쿠팡 제품 링크를 `리뷰할 제품/장소 정보 링크 여러 개` 칸에 넣었을 때, 공개 HTML에서 읽히는 상품명, 가격, 할인, 쿠폰, 배송, 상품평, 구매 반응, 판매자, 쿠팡상품번호, 제품 특징/혜택 후보를 최대한 가져와 글 생성에 반영합니다.

단, 쿠팡 상세페이지의 제품 설명이 이미지로만 되어 있으면 이미지 속 글자는 자동으로 읽지 못할 수 있습니다. 그럴 때는 중요한 제품 설명을 `제품 스펙/구성/용량` 또는 `특징 키워드` 칸에 직접 붙여넣으면 더 정확하게 작성됩니다.


## 블로그 템플릿 저장/링크 분석 기능

이번 버전에는 `0. 블로그 템플릿` 영역이 추가되었습니다. 직접 만든 글 구조를 여러 개 저장해서 불러올 수 있고, 참고하고 싶은 블로그 링크를 여러 개 넣으면 문단 순서, 소제목 흐름, 도입부/마무리 방식, 사진/영상 배치 방식만 분석해서 새 글에 반영합니다. 원문 문장은 그대로 복사하지 않도록 프롬프트에 안전 규칙을 넣었습니다. 자세한 내용은 `블로그_템플릿_사용법.md`를 확인하세요.

---

## Streamlit Cloud Secrets 전용 수정사항

이번 버전부터는 Streamlit Cloud의 Secrets를 우선해서 읽습니다.
따라서 Streamlit Cloud에 배포할 때는 `.env` 파일을 만들 필요가 없습니다.

Streamlit Cloud의 Secrets에는 아래처럼 입력하세요.

```toml
NVIDIA_API_KEY = "nvapi-새로발급받은키"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "minimaxai/minimax-m3"
NVIDIA_MAX_TOKENS = "2500"
NVIDIA_TIMEOUT_SECONDS = "240"
```

앱 왼쪽 사이드바에 `Streamlit Secrets에서 NVIDIA_API_KEY를 불러왔습니다.`라고 표시되면 정상입니다.
API Key 직접 입력칸은 비워둬도 됩니다.

실제 API 키가 들어간 `.streamlit/secrets.toml` 파일은 GitHub에 올리지 마세요.


## API 상태판 / 연결 테스트

앱 왼쪽 사이드바의 **API 상태판**에서 다음을 확인할 수 있습니다.

1. 현재 API 키가 어디에서 불러와졌는지 확인
   - Streamlit Secrets
   - `.env` / 환경변수
   - 화면 직접 입력
2. API 키 마스킹 표시
3. **현재 적용 API 키 확인** 입력칸의 오른쪽 눈 아이콘을 눌러 전체 키 확인
4. `base_url`, 실제 호출 주소, 모델명 확인
5. **API 연결 테스트** 버튼으로 정상/오류 판별

오류별 의미는 대략 아래와 같습니다.

```txt
401: API 키가 틀렸거나 만료됨 / Bearer를 잘못 넣음
403: 키는 맞지만 모델 권한 또는 계정 권한 문제
404: base_url 또는 엔드포인트 문제
429: 요청 제한, 무료 사용량, 속도 제한 문제
ReadTimeout: 서버 응답 지연 / max_tokens가 너무 큼
ConnectionError: 인터넷, 방화벽, VPN, 주소 문제
```

주의: Streamlit 앱을 공개 주소로 배포하면, 앱에 접속한 사람이 눈 아이콘으로 API 키를 볼 수 있습니다. 혼자 쓰는 앱이 아니라면 Streamlit Cloud 앱 공유 범위를 제한하거나, 키 보기 기능을 사용하지 않는 것을 권장합니다.
