import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";

import "@fontsource-variable/fraunces/opsz.css";
import "@fontsource-variable/fraunces/opsz-italic.css";
import "@fontsource-variable/inter/index.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./components/ui.css";

import { RouteErrorBoundary } from "./components/RouteErrorBoundary";
import { Toaster } from "./components/Toaster";
import { AuthenticatedApp } from "./layouts/AuthenticatedApp";
import { CatalogPage } from "./pages/CatalogPage";
import { CookbooksHomePage } from "./pages/CookbooksHomePage";
import { CookbookDetailPage } from "./pages/CookbookDetailPage";
import { AddRecipeWizardPage } from "./pages/AddRecipeWizardPage";
import { LoginPage } from "./pages/LoginPage";
import { PublicRecipePage } from "./pages/PublicRecipePage";
import { RecipeDetailPage } from "./pages/RecipeDetailPage";
import { RecipeEditPage } from "./pages/RecipeEditPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ReviewPage } from "./pages/ReviewPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
});

// Every top-level route carries an `errorElement`: a throw during render
// bubbles to the nearest one, and with none anywhere the reader just gets a
// white screen. Inside the shell the boundary sits on a pathless wrapper
// route around the pages, so a page that throws is replaced *in the shell's
// outlet* — the nav and sign-out stay usable — while a failure in the shell
// itself falls through to the boundary on "/".
const router = createBrowserRouter([
  { path: "/login", element: <LoginPage />, errorElement: <RouteErrorBoundary /> },
  { path: "/register", element: <RegisterPage />, errorElement: <RouteErrorBoundary /> },
  // Unauthenticated public-link view — a sibling of /login, NOT nested under
  // AuthenticatedApp: no session is required (or checked) to view it.
  {
    path: "/p/:token",
    element: <PublicRecipePage />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/",
    element: <AuthenticatedApp />,
    errorElement: <RouteErrorBoundary />,
    children: [
      {
        errorElement: <RouteErrorBoundary />,
        children: [
          { index: true, element: <CookbooksHomePage /> },
          { path: "cookbooks/:id", element: <CookbookDetailPage /> },
          { path: "add", element: <AddRecipeWizardPage /> },
          // Old ingestion entry points now live inside the wizard.
          { path: "inbox", element: <Navigate to="/add" replace /> },
          { path: "jobs/:id/review", element: <ReviewPage /> },
          { path: "recipes/new", element: <Navigate to="/add" replace /> },
          { path: "recipes/:id/edit", element: <RecipeEditPage /> },
          { path: "recipes/:id", element: <RecipeDetailPage /> },
          { path: "catalog", element: <CatalogPage /> },
        ],
      },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
