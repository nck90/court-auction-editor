var srcPath = arguments[0];
var outPath = arguments[1];

function main() {
  var doc = app.open(File(srcPath), false);
  var page = doc.pages[0];
  var lines = [];
  lines.push("page_bounds=" + page.bounds.join(","));
  for (var i = 0; i < page.textFrames.length; i++) {
    var tf = page.textFrames[i];
    var txt = "";
    try {
      txt = tf.parentStory.contents || "";
    } catch (e) {}
    txt = txt.replace(/\r/g, "\\n");
    if (txt.length > 120) txt = txt.substring(0, 120);
    lines.push([
      "idx=" + i,
      "id=" + tf.id,
      "bounds=" + tf.geometricBounds.join(","),
      "story=" + (tf.parentStory ? tf.parentStory.id : ""),
      "text=" + txt
    ].join(" | "));
  }
  var f = File(outPath);
  f.encoding = "UTF-8";
  f.open("w");
  f.write(lines.join("\n"));
  f.close();
  doc.close(SaveOptions.NO);
}

main();
