import type { PlanStep } from "../api/types";

export function PlanCard({
  goal,
  steps,
}: {
  goal: string | null;
  steps: PlanStep[];
}): React.JSX.Element {
  return (
    <section className="card" aria-label="Plan">
      <h3>Plan</h3>
      {goal ? <p className="muted">{goal}</p> : null}
      <ol className="steps">
        {steps.map((step, index) => (
          <li className="step" key={step.id ?? index}>
            <span className="idx">{index + 1}</span>
            <span className="desc">
              {step.description}
              {step.tool ? <span className="muted mono"> · {step.tool}</span> : null}
            </span>
            <span className={`pill ${step.required_permission}`}>{step.required_permission}</span>
          </li>
        ))}
      </ol>
      <p className="muted">
        Steps marked write/external/control pause for an approval that is single-use and bound to
        these exact parameters.
      </p>
    </section>
  );
}
