import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { apiErrorMessage } from "../api/errors";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import "./shares.css";

/** Cookbook browse page's index, but for the shared cookbooks a friend
 *  group keeps together — a create form up top, then a list of the ones
 *  the current user already belongs to. */
export function SharesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  const cookbooks = useQuery({
    queryKey: ["shared-cookbooks"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/shared-cookbooks");
      if (error) throw error;
      return data;
    },
  });

  const create = useMutation({
    mutationFn: async (newName: string) => {
      const { data, error } = await api.POST("/api/shared-cookbooks", { body: { name: newName } });
      if (error) throw error;
      return data;
    },
    onSuccess: async (created) => {
      setName("");
      await queryClient.invalidateQueries({ queryKey: ["shared-cookbooks"] });
      // A freshly created cookbook is empty — take the owner straight to it
      // to invite friends and add the first recipe.
      navigate(`/shares/${created.id}`);
    },
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || create.isPending) return;
    create.mutate(trimmed);
  }

  const items = cookbooks.data ?? [];

  return (
    <>
      <PageHeader
        overline="Friends & family"
        title="Shares"
        subtitle="Cookbooks you keep together — everyone in it can add and edit recipes."
      />

      <form className="shares-create" onSubmit={handleSubmit}>
        <label className="shares-create__label" htmlFor="shares-new-name">
          New shared cookbook
        </label>
        <div className="shares-create__row">
          <input
            id="shares-new-name"
            className="input shares-create__input"
            placeholder="e.g. Sunday Dinners"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <Button type="submit" disabled={!name.trim() || create.isPending}>
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
        {create.isError ? (
          <p className="shares-create__error" role="alert">
            {apiErrorMessage(create.error, "Could not create this shared cookbook.")}
          </p>
        ) : null}
      </form>

      {cookbooks.isPending ? (
        <Skeleton variant="rows" count={3} />
      ) : cookbooks.isError ? (
        <ErrorState
          message={apiErrorMessage(cookbooks.error, "Could not load your shared cookbooks.")}
          onRetry={() => void cookbooks.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="No shared cookbooks yet"
          body="Start one above — everyone you invite can add recipes and see each other's edits."
        />
      ) : (
        <ul className="shares-list">
          {items.map((cookbook) => (
            <li key={cookbook.id}>
              <Link to={`/shares/${cookbook.id}`} className="shares-list__item">
                <span className="shares-list__name">{cookbook.name}</span>
                <span className="shares-list__meta">
                  {cookbook.member_count} {cookbook.member_count === 1 ? "member" : "members"}
                  {" · "}
                  {cookbook.recipe_count} {cookbook.recipe_count === 1 ? "recipe" : "recipes"}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
