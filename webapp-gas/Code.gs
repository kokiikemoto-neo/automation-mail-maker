/**
 * Writing Robot 営業ツール — GAS ウェブアプリ（サーバー側）
 *
 * 役割：
 *   ①誰でも使えるウェブ画面を配信する（doGet）
 *   ②Claude API で手書きDMの文面を自動生成する（generateMessage）
 *   ③「便箋作成」で注文をスプレッドシート（キュー）に積む（createOrder）
 *   ④最近の注文の状況を返す（getOrders）
 *
 * ロボットPC側（Python常駐）は、このスプレッドシートを数秒ごとに見に行き、
 * ステータス「待ち」の行を見つけたら手書きして「完了」に更新する（ポーリング方式）。
 *
 * ── 事前設定（1回だけ）──────────────────────────────
 *   1. このスクリプトを「コンテナバインド」ではなく、対象スプレッドシートに
 *      紐づけて使う場合は SHEET_ID を空のままでOK（getActiveSpreadsheet を使う）。
 *      単独スクリプトの場合は SHEET_ID にスプレッドシートIDを入れる。
 *   2. プロジェクトの設定 → スクリプト プロパティ に
 *        GEMINI_API_KEY = AIza... を登録する（APIキーは画面に出さない）
 *        ※ キーは aistudio.google.com/apikey で取得
 *   3. デプロイ → 新しいデプロイ → 種類「ウェブアプリ」
 *        実行するユーザー：自分
 *        アクセスできるユーザー：全員
 * ──────────────────────────────────────────────
 */

// ===== 設定 =====
var SHEET_ID = '';                 // 単独スクリプトのときだけ、対象スプレッドシートIDを入れる
var ORDERS_SHEET = 'Orders';       // 注文キューのシート名
var COMPANIES_SHEET = 'Companies'; // 会社マスタ（任意）のシート名
var MINUTES_SHEET = 'Minutes';     // 議事録の保存シート
var CONTROL_SHEET = 'Control';     // ロボット操作（一時停止/再開/中止）の連絡シート
var MINUTES_FOLDER = '便箋_議事録ファイル';  // 添付ファイルの保存先 Drive フォルダ名
var PAST_MINUTES_LIMIT = 3;        // 背景として自動参照する、その会社の過去議事録の件数
var GEMINI_MODEL = 'gemini-2.5-flash'; // 文面生成のモデル（無料枠あり・速い。品質を上げるなら gemini-2.5-pro）

// 注文キューの列（ロボットPC側との「契約」。順番を変えたら両方直すこと）
var COL = {
  ID: 1,        // A: 注文ID
  RECEIVED: 2,  // B: 受付時刻
  STAFF: 3,     // C: 担当者
  COMPANY: 4,   // D: 会社名
  RECIPIENT: 5, // E: 宛名（氏名／会社単独なら空でも可）
  MESSAGE: 6,   // F: 文面（手書きする本文）
  STATUS: 7,    // G: ステータス（待ち / 処理中 / 完了 / エラー）
  PROCESSED: 8, // H: 処理時刻
  NOTE: 9       // I: 備考（エラー内容など）
};
var HEADERS = ['ID', '受付時刻', '担当者', '会社名', '宛名', '文面', 'ステータス', '処理時刻', '備考'];


// ===== 画面配信 =====
function doGet() {
  return HtmlService.createHtmlOutputFromFile('index')
    .setTitle('便箋作成ツール')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


// ===== スプレッドシート補助 =====
// 優先順位：①コード内 SHEET_ID → ②コンテナバインドの親シート →
//          ③スクリプトプロパティに記憶したID → ④無ければ自動作成して記憶
function getSpreadsheet_() {
  if (SHEET_ID) return SpreadsheetApp.openById(SHEET_ID);

  var active = SpreadsheetApp.getActiveSpreadsheet();
  if (active) return active;

  var props = PropertiesService.getScriptProperties();
  var saved = props.getProperty('SHEET_ID');
  if (saved) {
    try { return SpreadsheetApp.openById(saved); } catch (e) { /* 消えていたら作り直す */ }
  }

  // スタンドアロン型：専用のスプレッドシートを自動作成して以後ずっと使う
  var ss = SpreadsheetApp.create('便箋作成キュー（自動生成）');
  props.setProperty('SHEET_ID', ss.getId());
  return ss;
}

function getOrdersSheet_() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(ORDERS_SHEET);
  if (!sh) {
    sh = ss.insertSheet(ORDERS_SHEET);
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}


// ===== ① 文面の自動生成（Gemini API）=====
/**
 * 会社情報をもとに、手書きDMの文面（短め）を生成して返す。
 * @param {{company:string, recipient:string, hint:string}} input
 * @return {{ok:boolean, message:string, error:string}}
 */
// Gemini を1回呼ぶ共通処理（末尾 _ なので画面からは直接呼べない内部関数）
// parts は Gemini の parts 配列（[{text:...}] や [{text},{inline_data}] ）
function callGemini_(system, parts) {
  var apiKey = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
  if (!apiKey) {
    return { ok: false, message: '', error: 'GEMINI_API_KEY が未設定です（スクリプト プロパティに登録してください）' };
  }

  var payload = {
    system_instruction: { parts: [{ text: system }] },
    contents: [{ role: 'user', parts: parts }],
    generationConfig: {
      maxOutputTokens: 2048,
      temperature: 1,
      thinkingConfig: { thinkingBudget: 0 } // 思考を切って速く・安く・空応答を防ぐ
    }
  };

  var url = 'https://generativelanguage.googleapis.com/v1beta/models/' +
            encodeURIComponent(GEMINI_MODEL) + ':generateContent';

  var res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'x-goog-api-key': apiKey },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  var code = res.getResponseCode();
  var body = JSON.parse(res.getContentText());
  if (code !== 200) {
    return { ok: false, message: '', error: 'Gemini API エラー(' + code + '): ' + (body.error && body.error.message || res.getContentText()) };
  }
  if (body.promptFeedback && body.promptFeedback.blockReason) {
    return { ok: false, message: '', error: '生成がブロックされました: ' + body.promptFeedback.blockReason };
  }

  var cand = (body.candidates || [])[0];
  var text = cand && cand.content && cand.content.parts
    ? cand.content.parts.map(function (p) { return p.text || ''; }).join('').trim()
    : '';
  if (!text) {
    return { ok: false, message: '', error: '空の応答でした（finishReason: ' + (cand && cand.finishReason || '不明') + '）' };
  }
  return { ok: true, message: text, error: '' };
}


// ①-A 新規営業：会社情報から手書きDMの文面を作る
function generateMessage(input) {
  try {
    var company = (input && input.company || '').trim();
    var recipient = (input && input.recipient || '').trim();
    var hint = (input && input.hint || '').trim();
    if (!company) return { ok: false, message: '', error: '会社名を入力してください' };

    var system =
      'あなたはネオキャリアの法人営業担当です。中小企業の経営者・採用担当者に向けて、' +
      '手書きの便箋で送る短いDMの文面を作ります。\n' +
      '制約：\n' +
      '・日本語、120〜200字程度（便箋に手書きできる短さ）。\n' +
      '・テンプレ感を避け、その会社ならではの一言を入れる。\n' +
      '・採用課題・離職・人手不足など、相手の悩みにそっと寄り添うトーン。売り込みすぎない。\n' +
      '・宛名・時候の挨拶・署名は本文に含めない（別で書くため、本文だけ返す）。\n' +
      '・記号や絵文字は使わない。手紙としてそのまま書ける素直な文章にする。';

    var userText =
      '会社名：' + company + '\n' +
      (recipient ? '宛名：' + recipient + '\n' : '') +
      (hint ? '補足（この会社について分かっていること）：' + hint + '\n' : '') +
      '\n上記の会社に送る、手書き便箋の本文だけを書いてください。';

    return callGemini_(system, [{ text: userText }]);
  } catch (e) {
    return { ok: false, message: '', error: '生成中に例外: ' + e };
  }
}


// ①-B フォロー原稿：議事録ファイル（PC添付 or Driveから取り込み）＋過去の経緯から原稿を作る
/**
 * @param {{company:string, recipient:string,
 *          file:{name:string, mimeType:string, dataBase64:string},
 *          driveRef:string}} input
 * @return {{ok:boolean, message:string, error:string}}
 */
function generateDraftFromMinutes(input) {
  try {
    var company = (input && input.company || '').trim();
    var recipient = (input && input.recipient || '').trim();
    var file = input && input.file;
    var driveRef = (input && input.driveRef || '').trim();

    if (!(file && file.dataBase64) && !driveRef) {
      return { ok: false, message: '', error: '議事録ファイルを添付するか、ドライブのURL/IDを入れてください' };
    }

    // ファイルの取得元を統一：①Drive取り込み or ②PC添付 → blob にまとめる
    var minutes = '';       // テキスト系ファイルの中身（あれば）
    var fileSummary = '';   // Minutes シートに残す用
    var fileLink = '';      // Drive リンク
    var inlinePart = null;  // Gemini に渡す inline_data（PDF/画像/音声/動画）
    var blob = null, mimeType = '', alreadyInDrive = false;

    if (driveRef) {
      var fid = extractDriveId_(driveRef);
      var df = DriveApp.getFileById(fid);   // 実行者(あなた)がアクセスできるファイルのみ
      blob = df.getBlob();
      mimeType = blob.getContentType();
      fileSummary = df.getName();
      fileLink = df.getUrl();
      alreadyInDrive = true;
    } else {
      var bytes = Utilities.base64Decode(file.dataBase64);
      blob = Utilities.newBlob(bytes, file.mimeType, file.name);
      mimeType = file.mimeType;
      fileSummary = file.name;
    }

    if (!alreadyInDrive) fileLink = saveMinutesFile_(blob);  // 添付は Drive に保存して残す
    if (mimeType && mimeType.indexOf('text/') === 0) {
      minutes = blob.getDataAsString('UTF-8');               // テキストは読み取って本文に
    } else {
      inlinePart = { inline_data: { mime_type: mimeType, data: Utilities.base64Encode(blob.getBytes()) } };
    }

    // その会社の過去の議事録を背景として自動参照
    var bgCombined = company ? getPastMinutes_(company) : '';

    var system =
      'あなたはネオキャリアの法人営業担当です。商談の議事録（テキスト・PDF・画像・音声・動画などの資料）と、これまでの背景（過去の経緯）を読み取り、' +
      '相手にお礼とフォローを伝える「手書き便箋の原稿」を作ります。\n' +
      '制約：\n' +
      '・日本語、150〜250字程度（便箋に手書きできる長さ）。\n' +
      '・議事録の具体的な話題や、相手が気にしていた点に必ず触れ、「ちゃんと聞いていた」と伝わるようにする。\n' +
      '・これまでの背景があれば踏まえ、関係の積み重ねが感じられる自然な文章にする。\n' +
      '・お礼 → 議事録を踏まえた一言 → 次の一歩をそっと促す、の流れ。売り込みすぎない。\n' +
      '・宛名・時候の挨拶・署名は本文に含めない（別で書くため、本文だけ返す）。\n' +
      '・記号や絵文字は使わない。手紙としてそのまま書ける素直な文章にする。';

    var userText =
      (company ? '会社名：' + company + '\n' : '') +
      (recipient ? '宛名：' + recipient + '\n' : '') +
      (bgCombined ? '【これまでの背景・過去の経緯】\n' + bgCombined + '\n\n' : '') +
      (minutes ? '【今回の議事録】\n' + minutes + '\n\n' : '') +
      (inlinePart ? '【今回の議事録は添付資料（' + fileSummary + '）を参照】\n\n' : '') +
      '上記を踏まえ、手書き便箋に書くフォローの原稿（本文だけ）を書いてください。';

    var parts = [{ text: userText }];
    if (inlinePart) parts.push(inlinePart);

    var result = callGemini_(system, parts);

    // 成功したら Minutes シートに保存（議事録の蓄積。次回以降の背景に使われる）
    if (result.ok) {
      saveMinutesRow_({
        company: company,
        recipient: recipient,
        minutes: minutes || ('（添付: ' + fileSummary + '）'),
        fileLink: fileLink,
        draft: result.message
      });
    }
    return result;
  } catch (e) {
    return { ok: false, message: '', error: '生成中に例外: ' + e };
  }
}


// ===== 議事録の保存（別シート Minutes ＋ Drive）=====
function getMinutesSheet_() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(MINUTES_SHEET);
  if (!sh) {
    sh = ss.insertSheet(MINUTES_SHEET);
    var head = ['日時', '会社名', '宛名', '議事録', '添付ファイル', '生成した原稿'];
    sh.getRange(1, 1, 1, head.length).setValues([head]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function saveMinutesRow_(rec) {
  var sh = getMinutesSheet_();
  sh.appendRow([new Date(), rec.company, rec.recipient, rec.minutes, rec.fileLink, rec.draft]);
}

function saveMinutesFile_(blob) {
  var folders = DriveApp.getFoldersByName(MINUTES_FOLDER);
  var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(MINUTES_FOLDER);
  var f = folder.createFile(blob);
  return f.getUrl();
}

// Drive の URL かファイルIDから、ファイルIDだけを取り出す
function extractDriveId_(ref) {
  var m = ref.match(/\/d\/([a-zA-Z0-9_-]+)/) || ref.match(/[?&]id=([a-zA-Z0-9_-]+)/);
  return m ? m[1] : ref;   // どちらにも当たらなければ、そのものをIDとみなす
}

// その会社の過去議事録（直近 PAST_MINUTES_LIMIT 件）を、背景テキストとしてまとめて返す
function getPastMinutes_(company) {
  try {
    var ss = getSpreadsheet_();
    var sh = ss.getSheetByName(MINUTES_SHEET);
    if (!sh || sh.getLastRow() < 2) return '';
    var values = sh.getRange(2, 1, sh.getLastRow() - 1, 6).getValues();
    var hits = [];
    for (var i = values.length - 1; i >= 0 && hits.length < PAST_MINUTES_LIMIT; i--) {
      var row = values[i];
      if (('' + row[1]).trim() === company) {
        var when = row[0] ? Utilities.formatDate(new Date(row[0]), 'Asia/Tokyo', 'yyyy/MM/dd') : '';
        var mins = ('' + row[3]).trim();
        if (mins) hits.push('（' + when + '）' + mins);
      }
    }
    return hits.reverse().join('\n');
  } catch (e) {
    return '';
  }
}


// ===== ② 注文をキューに積む（便箋作成）=====
/**
 * @param {{staff:string, company:string, recipient:string, message:string}} order
 * @return {{ok:boolean, id:string, error:string}}
 */
function createOrder(order) {
  try {
    var company = (order && order.company || '').trim();
    var message = (order && order.message || '').trim();
    if (!company) return { ok: false, id: '', error: '会社名がありません' };
    if (!message) return { ok: false, id: '', error: '文面がありません' };

    var sh = getOrdersSheet_();
    var id = 'ORD-' + Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd-HHmmss') +
             '-' + Math.floor(Math.random() * 1000);
    var row = [];
    row[COL.ID - 1] = id;
    row[COL.RECEIVED - 1] = new Date();
    row[COL.STAFF - 1] = (order.staff || '').trim();
    row[COL.COMPANY - 1] = company;
    row[COL.RECIPIENT - 1] = (order.recipient || '').trim();
    row[COL.MESSAGE - 1] = message;
    row[COL.STATUS - 1] = '待ち';
    row[COL.PROCESSED - 1] = '';
    row[COL.NOTE - 1] = '';

    sh.appendRow(row);
    return { ok: true, id: id, error: '' };
  } catch (e) {
    return { ok: false, id: '', error: '登録中に例外: ' + e };
  }
}


// ===== ③ 最近の注文の状況を返す（画面表示用）=====
function getOrders(limit) {
  try {
    limit = limit || 15;
    var sh = getOrdersSheet_();
    var last = sh.getLastRow();
    if (last < 2) return { ok: true, rows: [] };

    var n = Math.min(limit, last - 1);
    var start = last - n + 1;
    var values = sh.getRange(start, 1, n, HEADERS.length).getValues();

    var rows = values.map(function (v) {
      return {
        id: v[COL.ID - 1],
        received: v[COL.RECEIVED - 1] ? Utilities.formatDate(new Date(v[COL.RECEIVED - 1]), 'Asia/Tokyo', 'MM/dd HH:mm') : '',
        staff: v[COL.STAFF - 1],
        company: v[COL.COMPANY - 1],
        recipient: v[COL.RECIPIENT - 1],
        status: v[COL.STATUS - 1]
      };
    }).reverse(); // 新しい順

    return { ok: true, rows: rows };
  } catch (e) {
    return { ok: false, rows: [], error: '' + e };
  }
}


// ===== ロボット操作（一時停止 / 再開 / 中止）=====
// Control シートを介して、画面の操作をロボットPCの常駐プログラムに伝える。
//   B1 指示     : 画面が書く（'' / 一時停止 / 再開 / 中止）
//   B2 状態     : 常駐プログラムが書く（待機中 / 書き込み中 / 一時停止中）
//   B3 処理中   : 常駐プログラムが書く（処理中の注文ID）
//   B4 更新時刻 : 常駐プログラムが書く
function getControlSheet_() {
  var ss = getSpreadsheet_();
  var sh = ss.getSheetByName(CONTROL_SHEET);
  if (!sh) {
    sh = ss.insertSheet(CONTROL_SHEET);
    sh.getRange(1, 1, 4, 2).setValues([
      ['指示', ''],
      ['状態', '待機中'],
      ['処理中の注文', ''],
      ['更新時刻', '']
    ]);
    sh.getRange(1, 1, 4, 1).setFontWeight('bold');
  }
  return sh;
}

// 画面の操作ボタンから呼ぶ。action は '一時停止' / '再開' / '中止'
function setControl(action) {
  try {
    var ok = ['一時停止', '再開', '中止'];
    if (ok.indexOf(action) < 0) return { ok: false, error: '不正な操作です' };
    getControlSheet_().getRange(1, 2).setValue(action);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: '' + e };
  }
}

// 画面表示用：いまのロボットの状態を返す
function getRobotState() {
  try {
    var sh = getControlSheet_();
    var v = sh.getRange(1, 2, 4, 1).getValues();
    return {
      ok: true,
      action: v[0][0],
      status: v[1][0] || '不明',
      current: v[2][0] || '',
      updated: v[3][0] ? Utilities.formatDate(new Date(v[3][0]), 'Asia/Tokyo', 'MM/dd HH:mm:ss') : ''
    };
  } catch (e) {
    return { ok: false, status: '不明', current: '', action: '', updated: '' };
  }
}


// ===== 会社マスタ（任意）=====
// Companies シートの A列に会社名を並べておくと、画面の候補に出せる。
function getCompanies() {
  try {
    var ss = getSpreadsheet_();
    var sh = ss.getSheetByName(COMPANIES_SHEET);
    if (!sh || sh.getLastRow() < 1) return { ok: true, companies: [] };
    var values = sh.getRange(1, 1, sh.getLastRow(), 1).getValues();
    var companies = values.map(function (v) { return ('' + v[0]).trim(); })
                          .filter(function (s) { return s && s !== '会社名'; });
    return { ok: true, companies: companies };
  } catch (e) {
    return { ok: true, companies: [] };
  }
}


// ===== 動作確認用（GASエディタから実行して、初期化＆権限承認をする）=====
function setup() {
  getOrdersSheet_(); // Orders シートを作る
  Logger.log('Orders シートを準備しました。次に generateMessage_test を実行してください。');
}

// 権限の再承認用：Sheets と Drive を実際に「読み書き」して、必要な権限をまとめて承認させる。
// スコープ（Drive 等）を追加したあとは、エディタでこの関数を1回実行して承認してください。
function authorize() {
  getOrdersSheet_();                                 // Sheets 権限
  // Drive の読み書き権限まで承認させる：フォルダ作成 → ファイル作成 → 取得 → 削除
  var folders = DriveApp.getFoldersByName(MINUTES_FOLDER);
  var folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(MINUTES_FOLDER);
  var f = folder.createFile(Utilities.newBlob('authorize test', 'text/plain', 'authorize_test.txt'));
  DriveApp.getFileById(f.getId());                   // 取り込みで使う getFileById も承認
  f.setTrashed(true);                                // 後始末（テストファイルを削除）
  Logger.log('権限の承認が完了しました（Sheets + Drive 読み書き）。Drive 取り込みが使えます。');
}

function generateMessage_test() {
  var r = generateMessage({ company: '株式会社サンプル', recipient: '採用ご担当者', hint: '飲食チェーンを多店舗展開' });
  Logger.log(JSON.stringify(r, null, 2));
}
