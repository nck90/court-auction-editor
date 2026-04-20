var srcPath = arguments[0];
var payloadPath = arguments[1];
var outIndd = arguments[2];
var outPdf = arguments[3];

function readJson(path) {
  var f = File(path);
  f.encoding = "UTF-8";
  f.open("r");
  var txt = f.read();
  f.close();
  return eval("(" + txt + ")");
}

function normalizeText(s) {
  return (s || "").replace(/\r/g, "\n");
}

function slotNameForLabel(contents) {
  contents = normalizeText(contents);
  if (contents.indexOf("[기타]") >= 0) return "기타";
  if (contents.indexOf("[아파트]") >= 0) return "아파트";
  if (contents.indexOf("[대지/임야/전답]") >= 0) return "대지/임야/전답";
  if (contents.indexOf("[상가/오피스텔,근린시설]") >= 0) return "상가/오피스텔,근린시설";
  if (contents.indexOf("[연립주택/다세대/빌라]") >= 0) return "연립주택/다세대/빌라";
  if (contents.indexOf("[단독주택,다가구주택]") >= 0) return "단독주택,다가구주택";
  return null;
}

function findSlots(page) {
  var labels = {};
  var bodies = [];
  for (var i = 0; i < page.textFrames.length; i++) {
    var tf = page.textFrames[i];
    var txt = "";
    try { txt = tf.parentStory.contents || ""; } catch (e) {}
    var name = slotNameForLabel(txt);
    if (name) {
      labels[name] = tf;
    } else if (tf.parentStory && tf.parentStory.tables.length > 0) {
      bodies.push(tf);
    }
  }

  var slots = {};
  for (var group in labels) {
    var label = labels[group];
    var lb = label.geometricBounds;
    var best = null;
    var bestDist = 999999;
    for (var j = 0; j < bodies.length; j++) {
      var body = bodies[j];
      var bb = body.geometricBounds;
      var dx = Math.abs(bb[1] - lb[1]);
      var dy = bb[0] - lb[2];
      if (dy >= -0.5 && dx < 2 && dy < bestDist) {
        best = body;
        bestDist = dy;
      }
    }
    slots[group] = {label: label, body: best};
  }
  return slots;
}

function ensureSecondPage(doc) {
  if (doc.pages.length < 2) {
    doc.pages[0].duplicate(LocationOptions.AFTER, doc.pages[0]);
  }
}

function setFrameEmpty(tf) {
  if (!tf) return;
  tf.contents = "";
}

function fitTable(table, rowCount) {
  while (table.rows.length > rowCount) table.rows.lastItem().remove();
  while (table.rows.length < rowCount) table.rows.add(LocationOptions.AT_END, table.rows.lastItem());
}

function fillTable(tf, rows, group) {
  if (!tf) return;
  if (!rows || rows.length === 0) {
    tf.contents = "";
    return;
  }
  var table = tf.parentStory.tables[0];
  if (!table) return;
  fitTable(table, rows.length);
  for (var r = 0; r < rows.length; r++) {
    for (var c = 0; c < 6; c++) {
      table.rows[r].cells[c].contents = rows[r][c];
    }
  }
}

function applyPage(page, pagePayload) {
  var slots = findSlots(page);
  var order = ["기타","아파트","대지/임야/전답","상가/오피스텔,근린시설","연립주택/다세대/빌라","단독주택,다가구주택"];
  for (var i = 0; i < order.length; i++) {
    var group = order[i];
    var slot = slots[group];
    if (!slot) continue;
    var rows = pagePayload[group] || [];
    if (rows.length === 0) {
      setFrameEmpty(slot.label);
      setFrameEmpty(slot.body);
    } else {
      slot.label.contents = "[" + group + "]";
      fillTable(slot.body, rows, group);
    }
  }
}

function main() {
  var payload = readJson(payloadPath);
  var doc = app.open(File(srcPath), false);
  ensureSecondPage(doc);
  applyPage(doc.pages[0], payload.pages[0]);
  applyPage(doc.pages[1], payload.pages[1]);
  doc.save(File(outIndd));

  var preset;
  try {
    preset = app.pdfExportPresets.itemByName("[High Quality Print]");
    preset.name;
  } catch (e) {
    preset = app.pdfExportPresets.firstItem();
  }
  doc.exportFile(ExportFormat.PDF_TYPE, File(outPdf), false, preset);
  doc.close(SaveOptions.YES);
}

main();
