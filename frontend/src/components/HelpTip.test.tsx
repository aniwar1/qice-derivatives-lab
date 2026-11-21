import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HelpTip } from "./HelpTip";

describe("HelpTip", () => {
  it("links a keyboard-focusable question mark to its explanation", () => {
    render(<HelpTip label="解释部分日线">部分日线不能宣称收盘确认。</HelpTip>);
    const trigger = screen.getByRole("button", { name: "解释部分日线" });
    const tooltip = screen.getByRole("tooltip");
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("部分日线不能宣称收盘确认。");
  });
});
