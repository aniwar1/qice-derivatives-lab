import "@testing-library/jest-dom/vitest";

class EventSourceStub {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public readonly url: string) {}
  addEventListener() {}
  close() {}
}

Object.defineProperty(globalThis, "EventSource", {
  value: EventSourceStub,
  writable: true,
});
