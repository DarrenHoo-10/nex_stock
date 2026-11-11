/** Map browser pixels through the rendered viewBox, including its centered margins. */
export function svgPointer(svg: SVGSVGElement, clientX: number, clientY: number): DOMPoint | null {
  const matrix = svg.getScreenCTM();
  if (!matrix) return null;
  return new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
}
