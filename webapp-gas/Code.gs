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
function generateMessage(input) {
  try {
    var apiKey = PropertiesService.getScriptProperties().getProperty('GEMINI_API_KEY');
    if (!apiKey) {
      return { ok: false, message: '', error: 'GEMINI_API_KEY が未設定です（スクリプト プロパティに登録してください）' };
    }

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

    var payload = {
      system_instruction: { parts: [{ text: system }] },
      contents: [{ role: 'user', parts: [{ text: userText }] }],
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

    // 安全フィルタ等でブロックされた場合
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
  } catch (e) {
    return { ok: false, message: '', error: '生成中に例外: ' + e };
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

function generateMessage_test() {
  var r = generateMessage({ company: '株式会社サンプル', recipient: '採用ご担当者', hint: '飲食チェーンを多店舗展開' });
  Logger.log(JSON.stringify(r, null, 2));
}
