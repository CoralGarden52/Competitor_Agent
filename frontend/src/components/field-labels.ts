export const CORE_FIELD_LABELS_ZH: Record<string, string> = {
  feature_tree: "功能树",
  strengths: "优势",
  weaknesses: "劣势",
  pricing_model: "定价模式",
  user_feedback: "用户反馈",
};

export function fieldLabelZh(fieldName: string): string {
  const key = String(fieldName || "").trim();
  if (!key) return "";
  if (CORE_FIELD_LABELS_ZH[key]) return CORE_FIELD_LABELS_ZH[key];
  return key.replace(/_/g, " ");
}
