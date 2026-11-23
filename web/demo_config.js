/**
 * Noodle Traffic Control — Presentation & Deployment Configuration
 *
 * For investor presentations on GitHub Pages:
 *   1. Paste your temporary Tranzy Open Data API key into TRANZY_API_KEY below.
 *   2. Anyone opening the link can switch to "Terra (Real)" immediately with zero setup
 *      (the key stays masked inside the UI modal).
 *   3. After your presentation, remove/rotate this temporary key on tranzy.ai/opendata.
 */
window.NOODLE_CONFIG = {
  TRANZY_API_KEY: "",
  TRANZY_AGENCY_ID: "1", // 1 = CTP Iași
  API_BASE_URL: "", // Leave empty to auto-detect local FastAPI or run standalone on GitHub Pages
};
