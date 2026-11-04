export function DiffView({ diff }: { diff: string }): React.JSX.Element {
  const lines = diff.split("\n");
  return (
    <pre className="diff" aria-label="Proposed change">
      {lines.map((line, index) => {
        const className = line.startsWith("+") && !line.startsWith("++")
          ? "add"
          : line.startsWith("-") && !line.startsWith("---")
            ? "del"
            : "ctx";
        return (
          <span className={className} key={index}>
            {line === "" ? " " : line}
          </span>
        );
      })}
    </pre>
  );
}
