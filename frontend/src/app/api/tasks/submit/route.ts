import { NextResponse } from "next/server";

type SubmitRequest = {
  text?: string;
};

type ThoughtStep = {
  id: string;
  title: string;
  status: "pending" | "running" | "done";
  detail?: string;
};

function buildSummary(text: string): string {
  if (text.length <= 56) {
    return `任务目标：${text}`;
  }
  return `任务目标：${text.slice(0, 56)}...`;
}

function buildThoughtSteps(text: string): ThoughtStep[] {
  return [
    {
      id: "step-1",
      title: "明确分析对象与核心问题",
      status: "done",
      detail: `已识别输入任务：${text.slice(0, 32)}${text.length > 32 ? "..." : ""}`,
    },
    {
      id: "step-2",
      title: "拆解对比维度并规划信息收集",
      status: "running",
      detail: "将围绕产品定位、功能、价格、渠道与用户反馈进行多维分析。",
    },
    {
      id: "step-3",
      title: "生成结构化洞察与建议",
      status: "pending",
      detail: "等待前置信息汇总完成后输出结论。",
    },
  ];
}

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as SubmitRequest;
    const text = body.text?.trim();

    if (!text) {
      return NextResponse.json({ message: "text 不能为空" }, { status: 400 });
    }

    const summary = buildSummary(text);
    const thoughtSteps = buildThoughtSteps(text);

    return NextResponse.json({
      summary,
      userMessage: text,
      thoughtSteps,
    });
  } catch {
    return NextResponse.json({ message: "请求解析失败" }, { status: 400 });
  }
}

// Future integration placeholders:
// GET /api/tasks/:id/thoughts
// GET /api/tasks/:id/messages
