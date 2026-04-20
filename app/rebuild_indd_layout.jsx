#target indesign

(function () {
  var logLines = [];
  function log(msg) { logLines.push(msg); }
  function flushLog() {
    try {
      var f = File("/tmp/rebuild_indd_debug.txt");
      f.encoding = "UTF-8";
      f.open("w");
      f.write(logLines.join("\n"));
      f.close();
    } catch (e) {}
  }
  function readRaw(path) {
    var f = File(path);
    f.encoding = "UTF-8";
    if (!f.exists) throw new Error("Missing file: " + path);
    f.open("r");
    var raw = f.read();
    f.close();
    return raw;
  }

  function readJson(path) {
    return eval("(" + readRaw(path) + ")");
  }

  function removeDynamicFrames(page) {
    var frames = page.textFrames.everyItem().getElements();
    for (var i = frames.length - 1; i >= 0; i--) {
      var item = frames[i];
      try {
        var gb = item.geometricBounds;
        var content = "";
        try { content = item.parentStory.contents || ""; } catch (e1) {}
        if (content.indexOf("법원 경매부동산의 매각 공고") >= 0) continue;
        if (content.indexOf("1.매각물건의 표시") >= 0) continue;
        if (content.indexOf("주의사항") >= 0) continue;
        if (content.indexOf("특별매각조건") >= 0) continue;
        if (gb[1] < 540 && gb[0] > 35) item.remove();
      } catch (e2) {}
    }
    var rects = page.rectangles.everyItem().getElements();
    for (var j = rects.length - 1; j >= 0; j--) {
      var rect = rects[j];
      try {
        var rgb = rect.geometricBounds;
        if (rgb[1] < 540 && rgb[0] > 35) rect.remove();
      } catch (e3) {}
    }
  }

  function applyKoreanFont(target) {
    var candidates = ["Apple SD Gothic Neo\tRegular", "AppleGothic\tRegular", "Arial Unicode MS\tRegular"];
    for (var i = 0; i < candidates.length; i++) {
      try {
        target.appliedFont = candidates[i];
        return;
      } catch (e) {}
    }
  }

  function makeHeading(page, bounds, text) {
    var tf = page.textFrames.add();
    tf.geometricBounds = bounds;
    tf.contents = text;
    tf.strokeWeight = 0;
    tf.fillColor = "None";
    return tf;
  }

  function placeSection(page, bodyBounds, imagePath) {
    var rect = page.rectangles.add();
    rect.geometricBounds = bodyBounds;
    rect.strokeWeight = 0;
    rect.fillColor = "None";
    rect.place(File(imagePath));
    rect.fit(FitOptions.CONTENT_TO_FRAME);
  }

  try {
    var args = readJson("/tmp/rebuild_indd_args.json");
    log("args ok");
    var payload = readJson(args.payloadPath);
    log("payload ok");
    var sections = payload["sections"];
    log("sections=" + (sections ? sections.length : "undefined"));
    if (sections && sections.length) {
      log("first=" + sections[0].group + "|" + sections[0].image);
    }
    var doc = app.open(File(args.srcPath), false);
    log("doc open");
    var page = doc.pages[0];

    removeDynamicFrames(page);
    log("removed");

    var headingMap = {
      "기타": [68, 8, 74, 244],
      "아파트": [142, 8, 148, 244],
      "연립주택/다세대/빌라": [168, 8, 174, 244],
      "대지/임야/전답": [42, 250, 48, 520],
      "상가/오피스텔,근린시설": [124, 250, 130, 520],
      "단독주택,다가구주택": [158, 250, 164, 520]
    };

    for (var i = 0; i < sections.length; i++) {
      log("section " + sections[i].group);
      makeHeading(page, headingMap[sections[i].group], "[" + sections[i].group + "]");
      log("heading ok " + sections[i].group);
      placeSection(page, sections[i].body, sections[i].image);
      log("image ok " + sections[i].group);
    }

    log("placed all");
    doc.save(File(args.inddPath));
    var preset;
    try {
      preset = app.pdfExportPresets.itemByName("[High Quality Print]");
      preset.name;
    } catch (e) {
      preset = app.pdfExportPresets.firstItem();
    }
    doc.exportFile(ExportFormat.PDF_TYPE, File(args.pdfPath), false, preset);
    doc.close(SaveOptions.YES);
    log("done");
  } catch (err) {
    log("ERROR: " + err);
    flushLog();
    throw err;
  }
  flushLog();
})();
