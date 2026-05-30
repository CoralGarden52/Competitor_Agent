import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "竞品分析智能体",
  description: "AI 驱动的竞品分析 Agent 协作系统"
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
