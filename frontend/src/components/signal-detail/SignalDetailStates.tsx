import { Button, EmptyState, Skeleton } from '../ui';

export function DetailLoading() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-6 w-48" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-4 w-72" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}

export function NotFound() {
  return (
    <EmptyState
      title="Signal not found."
      hint="It may have been removed, or the link is incorrect."
      action={
        <Button as="link" to="/signals" variant="secondary" size="sm">
          Back to feed
        </Button>
      }
    />
  );
}
