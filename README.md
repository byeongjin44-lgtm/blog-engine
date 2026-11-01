# blog-engine

여러 사이트의 블로그 글을 자동으로 작성하고, **텔레그램 승인 후에만** 발행하는 엔진입니다.

```
매일 08:47 (KST)   주제 선택 → Claude가 영어 글 작성 → 자동 품질검사 → 한국어 요약·기술 검토
                   → 텔레그램으로 [✅ 승인] [❌ 폐기] 버튼 전송
전송 후 최대 4시간   버튼을 기다리다가 누르는 즉시 처리 → 사이트 저장소에 커밋 → 사이트 자동 배포
매시간 (보조)       4시간이 지난 뒤 누른 버튼 처리
```

서버가 필요 없습니다. 모든 작업은 GitHub Actions에서 무료로 실행됩니다(공개 저장소 기준).

## 처음 설정 (한 번만)

### 1. 텔레그램 봇 만들기
스레드 자동화 봇과 섞이지 않도록 **새 봇**을 만드는 것을 권장합니다.

1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 이름 입력
2. 받은 **봇 토큰**을 메모 (`123456:ABC...` 형태)
3. 새 봇과의 대화창을 열고 아무 메시지나 보내기
4. 브라우저에서 `https://api.telegram.org/bot<봇토큰>/getUpdates` 열기 → `"chat":{"id":숫자` 의 숫자가 **Chat ID**

### 2. Claude API 키
[console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key

### 3. DevRivet 저장소에 글을 올릴 GitHub 토큰
1. GitHub 프로필 → **Settings** → 맨 아래 **Developer settings**
2. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
3. Repository access: **Only select repositories** → `devrivet`
4. Permissions → Repository permissions → **Contents: Read and write**
5. 생성 후 토큰을 메모 (만료일이 지나면 새로 발급해야 합니다)

### 4. blog-engine 저장소에 비밀값 등록
blog-engine 저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** 으로 4개 등록:

| 이름 | 값 |
| --- | --- |
| `ANTHROPIC_API_KEY` | 2번의 API 키 |
| `TELEGRAM_BOT_TOKEN` | 1번의 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 1번의 Chat ID |
| `TARGET_GITHUB_TOKEN` | 3번의 GitHub 토큰 |

### 5. 첫 실행 테스트
저장소 → **Actions** → **Draft posts** → **Run workflow**. 몇 분 뒤 텔레그램으로 검토 메시지가 옵니다.
승인 버튼을 누르면 몇 초 안에 "발행했습니다" 알림이 뜨고, 메시지 아래에 ✅ 발행됨이 붙습니다.
GitHub의 예약 실행은 몇 시간씩 늦어질 수 있어서, 매일 글이 오는 시각은 08:47보다 늦을 수 있습니다.
4시간이 지난 뒤 누른 버튼은 매시간 확인 작업이 처리하며, 바로 처리하려면 **Process approvals** → **Run workflow**.

## 운영

- **주제 추가**: `sites/devrivet/topics.toml` 맨 아래에 추가 (위에서부터 차례대로 사용)
- **하루 발행 수**: `sites/devrivet/site.toml`의 `posts_per_day`
- **폐기한 주제**는 다시 목록으로 돌아갑니다. 영구히 빼려면 topics.toml에서 지우세요.
- **원고 원문**: 텔레그램 메시지의 "전체 원고 보기" 또는 `state/drafts/`

## 사이트 추가

`sites/<사이트id>/site.toml`과 `topics.toml`을 만들면 됩니다. 워드프레스 사이트는 발행 모듈(`wordpress`)을 추가한 뒤 연결합니다.
