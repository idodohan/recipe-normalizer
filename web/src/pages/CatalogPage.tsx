import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";

export function CatalogPage() {
  return (
    <>
      <PageHeader
        overline="Reference"
        title="Catalog"
        subtitle="Canonical ingredients and units."
      />
      <EmptyState
        title="Catalog browsing is on its way"
        body="The ingredient and unit catalog will be browsable here shortly."
      />
    </>
  );
}
