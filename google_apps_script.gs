/**
 * 월간 내러티브 다이제스트 -> 구글 시트 자동 누적 (Apps Script 웹앱)
 *
 * 설치 방법:
 *  1) 데이터를 쌓을 구글 시트를 하나 엽니다 (새로 만들어도 됨).
 *  2) 상단 메뉴 [확장 프로그램] -> [Apps Script] 클릭.
 *  3) 기본 코드(function myFunction...)를 지우고 이 파일 내용을 통째로 붙여넣기.
 *  4) 아래 TOKEN 값을 원하는 아무 문자열로 바꾸세요 (GitHub Secret 과 동일하게).
 *  5) 우측 상단 [배포] -> [새 배포] -> 유형 [웹 앱] 선택
 *       - 실행 계정: 나
 *       - 액세스 권한: 모든 사용자(Anyone)
 *     -> [배포] -> 권한 승인 -> 나오는 '웹 앱 URL' 복사.
 *  6) 그 URL 을 GitHub Secret 'GSHEET_WEBHOOK_URL' 로,
 *     TOKEN 값을 GitHub Secret 'GSHEET_TOKEN' 으로 등록.
 */

// GitHub Secret 의 GSHEET_TOKEN 과 똑같이 맞추세요. (빈 문자열이면 검증 안 함)
var TOKEN = "change-this-secret-123";

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    if (TOKEN && body.token !== TOKEN) {
      return _json({ ok: false, error: "unauthorized" });
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var month = body.month || "";
    var total = body.total || 0;

    // 1) 티커 월별 시트
    var tSheet = _sheet(ss, "티커월별", ["년월", "티커", "언급횟수", "등장일수", "총감지건수"]);
    (body.tickers || []).forEach(function (row) {
      tSheet.appendRow([month, row[0], row[1], row[2], total]);
    });

    // 2) 테마 월별 시트
    var thSheet = _sheet(ss, "테마월별", ["년월", "테마", "언급횟수", "총감지건수"]);
    (body.themes || []).forEach(function (row) {
      thSheet.appendRow([month, row[0], row[1], total]);
    });

    return _json({
      ok: true,
      month: month,
      tickers: (body.tickers || []).length,
      themes: (body.themes || []).length
    });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

// 시트가 없으면 만들고 헤더를 넣어줌
function _sheet(ss, name, header) {
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(header);
  }
  return sh;
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
