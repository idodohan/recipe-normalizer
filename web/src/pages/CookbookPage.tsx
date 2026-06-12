import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function CookbookPage() {
  const navigate = useNavigate();
  return (
    <>
      <PageHeader
        overline="Your collection"
        title="Cookbook"
        subtitle="Recipes you have saved and normalized."
        action={
          <Button
            variant="secondary"
            onClick={() => navigate("/recipes/new")}
          >
            Add recipe
          </Button>
        }
      />
      <EmptyState
        title="Your cookbook is empty"
        body="When you add recipes, they will appear here — ingredients normalized, quantities ready to scale."
        action={
          <Button onClick={() => navigate("/recipes/new")}>Add a recipe</Button>
        }
      />
    </>
  );
}
