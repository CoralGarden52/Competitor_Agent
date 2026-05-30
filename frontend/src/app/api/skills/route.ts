import { promises as fs } from "node:fs";
import path from "node:path";

import { NextResponse } from "next/server";

type Skill = {
  name: string;
  description: string;
  path: string;
};

function parseFrontmatter(raw: string): { name: string; description: string } {
  const nameMatch = raw.match(/^name:\s*(.+)$/m);
  const descriptionMatch = raw.match(/^description:\s*(.+)$/m);
  return {
    name: nameMatch?.[1]?.trim() ?? "unknown-skill",
    description: descriptionMatch?.[1]?.trim() ?? ""
  };
}

export async function GET() {
  const skillsRoot = path.resolve(process.cwd(), "..", "skills");

  try {
    const entries = await fs.readdir(skillsRoot, { withFileTypes: true });
    const skills: Skill[] = [];

    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const skillPath = path.join(skillsRoot, entry.name, "SKILL.md");
      try {
        const content = await fs.readFile(skillPath, "utf-8");
        const parsed = parseFrontmatter(content);
        skills.push({
          name: parsed.name,
          description: parsed.description,
          path: skillPath
        });
      } catch {
        skills.push({
          name: entry.name,
          description: "",
          path: skillPath
        });
      }
    }

    skills.sort((a, b) => a.name.localeCompare(b.name));
    return NextResponse.json({ skills });
  } catch (error) {
    return NextResponse.json(
      {
        skills: [],
        error: error instanceof Error ? error.message : "Failed to load skills"
      },
      { status: 200 }
    );
  }
}
