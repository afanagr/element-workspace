declare module 'mammoth/mammoth.browser' {
  export function convertToHtml(
    input: { arrayBuffer: ArrayBuffer },
    options?: Record<string, unknown>,
  ): Promise<{ value: string; messages: { type: string; message: string }[] }>;
  const mammoth: {
    convertToHtml: typeof convertToHtml;
  };
  export default mammoth;
}

