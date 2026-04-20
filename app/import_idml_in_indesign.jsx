var idmlPath = arguments[0];
var inddPath = arguments[1];
var pdfPath = arguments[2];

var prevLevel = app.scriptPreferences.userInteractionLevel;
app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;

try {
  var doc = app.open(File(idmlPath), false);
  doc.save(File(inddPath));

  var preset;
  try {
    preset = app.pdfExportPresets.itemByName("[High Quality Print]");
    preset.name;
  } catch (e) {
    preset = app.pdfExportPresets.firstItem();
  }

  doc.exportFile(ExportFormat.PDF_TYPE, File(pdfPath), false, preset);
  doc.close(SaveOptions.YES);
} finally {
  app.scriptPreferences.userInteractionLevel = prevLevel;
}
