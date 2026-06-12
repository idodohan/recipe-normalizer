import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function CookbookPage() {
  return (
    <>
      <PageHeader
        overline="Your collection"
        title="Cookbook"
        subtitle="Recipes you have saved and normalized."
      />
      <EmptyState
        title="Your cookbook is empty"
        body="When you add recipes, they will appear here — ingredients normalized, quantities ready to scale."
      />
    </>
  );
}
