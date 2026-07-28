import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Skeleton } from "../components/Skeleton";
import { CookbookCard } from "../components/cookbook/CookbookCard";
import { NewCookbookDialog } from "../components/cookbook/NewCookbookDialog";
import { useCookbooks } from "../hooks/useCookbooks";
import { apiErrorMessage } from "../api/errors";
import "../components/cookbook/cookbooks-home.css";

export function CookbooksHomePage() {
  const navigate = useNavigate();
  const cookbooks = useCookbooks();
  const [newOpen, setNewOpen] = useState(false);

  const items = cookbooks.data ?? [];
  const mine = items.filter((c) => c.role === "owner");
  const shared = items.filter((c) => c.role !== "owner");
  const totalRecipes = items.reduce((n, c) => n + c.recipe_count, 0);

  return (
    <>
      <header className="cb-home__masthead">
        <div>
          <p className="cb-home__overline">Your kitchen</p>
          <h1 className="cb-home__title">Cookbooks</h1>
        </div>
        <div className="cb-home__actions">
          <Button variant="secondary" onClick={() => setNewOpen(true)}>
            New cookbook
          </Button>
          <Button onClick={() => navigate("/add")}>Add a recipe</Button>
        </div>
      </header>

      {cookbooks.isPending ? (
        <Skeleton variant="card-grid" />
      ) : cookbooks.isError ? (
        <ErrorState
          message={apiErrorMessage(
            cookbooks.error,
            "Could not load your cookbooks.",
          )}
          onRetry={() => void cookbooks.refetch()}
        />
      ) : totalRecipes === 0 && items.length <= 1 ? (
        <EmptyState
          title="Start your first cookbook"
          body="Paste a link, drop a PDF or photo, or type a recipe in — we normalize every amount to grams and millilitres and file it in a cookbook you can keep private or share."
          action={
            <div className="cb-home__firstrun">
              <Button onClick={() => navigate("/add")}>Add a recipe</Button>
              <Button variant="secondary" onClick={() => setNewOpen(true)}>
                New cookbook
              </Button>
            </div>
          }
        />
      ) : (
        <>
          <section className="cb-home__section">
            {shared.length > 0 ? (
              <h2 className="cb-home__section-title">Yours</h2>
            ) : null}
            <ul className="cb-grid">
              {mine.map((c) => (
                <li key={c.id}>
                  <CookbookCard cookbook={c} />
                </li>
              ))}
            </ul>
          </section>

          {shared.length > 0 ? (
            <section className="cb-home__section">
              <h2 className="cb-home__section-title">Shared with you</h2>
              <ul className="cb-grid">
                {shared.map((c) => (
                  <li key={c.id}>
                    <CookbookCard cookbook={c} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      )}

      <NewCookbookDialog open={newOpen} onClose={() => setNewOpen(false)} />
    </>
  );
}
