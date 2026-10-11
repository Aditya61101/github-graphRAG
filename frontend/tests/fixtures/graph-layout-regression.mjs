// Valid topology that threw Dagre 3.1.1's rectangle-intersection exception
// with the Explorer's original layout configuration: parallel + reverse edges.
export const layoutRegressionGraph = {
  nodes: Array.from({ length: 8 }, (_, index) => ({
    id: `entity:${index}`,
    label: `Service ${index}`,
    type: "Service",
    metadata: {},
  })),
  edges: [
    [5, 7],
    [7, 5],
    [5, 7],
    [5, 6],
  ].map(([source, target], index) => ({
    id: `edge:${source}:RELATION_${index}:${target}`,
    source: `entity:${source}`,
    target: `entity:${target}`,
    type: `RELATION_${index}`,
    metadata: {},
  })),
};
