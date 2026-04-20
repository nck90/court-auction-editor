#target indesign

(function () {
  function readArgsFile(path) {
    var f = File(path);
    if (!f.exists) return null;
    f.encoding = "UTF-8";
    f.open("r");
    var raw = f.read();
    f.close();
    return raw;
  }

  function extractValue(raw, key) {
    var re = new RegExp('"' + key + '"\\s*:\\s*"([^"]+)"');
    var match = raw.match(re);
    return match ? match[1] : "";
  }

  var rawArgs = readArgsFile("/tmp/inspect_indd_args.json");
  var docPath = extractValue(rawArgs || "", "docPath");
  var outPath = extractValue(rawArgs || "", "outPath");
  if (!docPath || !outPath) {
    throw new Error("docPath and outPath are required");
  }

  var doc = app.open(File(docPath), false);
  var lines = [];
  lines.push("DOC|" + doc.name + "|pages=" + doc.pages.length + "|stories=" + doc.stories.length + "|textFrames=" + doc.textFrames.length);

  for (var i = 0; i < doc.textFrames.length; i++) {
    var tf = doc.textFrames[i];
    var content = "";
    try {
      content = tf.parentStory.contents || "";
    } catch (e) {}
    if (content.length > 250) content = content.slice(0, 250);
    content = content.replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    lines.push("FRAME|" + (i + 1) + "|id=" + tf.id + "|storyId=" + tf.parentStory.id + "|bounds=" + tf.geometricBounds.join(",") + "|label=" + tf.label + "|content=" + content);
  }

  for (var j = 0; j < doc.stories.length; j++) {
    var st = doc.stories[j];
    var frameIds = [];
    for (var k = 0; k < st.textContainers.length; k++) {
      frameIds.push(st.textContainers[k].id);
    }
    var storyContent = st.contents || "";
    if (storyContent.length > 400) storyContent = storyContent.slice(0, 400);
    storyContent = storyContent.replace(/\r/g, "\\r").replace(/\n/g, "\\n");
    lines.push("STORY|" + (j + 1) + "|id=" + st.id + "|frameIds=" + frameIds.join(",") + "|length=" + st.contents.length + "|content=" + storyContent);
  }

  var outFile = File(outPath);
  outFile.encoding = "UTF-8";
  outFile.open("w");
  outFile.write(lines.join("\n"));
  outFile.close();

  doc.close(SaveOptions.NO);
})();
