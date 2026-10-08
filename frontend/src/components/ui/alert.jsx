import * as React from "react";
import { cva } from "class-variance-authority";
import { cn } from "@/lib/utils";

const alertVariants = cva(
  "relative w-full rounded-lg border px-4 py-3 text-sm grid grid-cols-[0_1fr] has-[>svg]:grid-cols-[calc(var(--spacing)*4)_1fr] gap-x-3 gap-y-1 items-start [&>svg]:size-4 [&>svg]:translate-y-0.5 [&>svg]:text-current",
  {
    variants: {
      variant: {
        default: "bg-card text-card-foreground border-border",
        destructive:
          "text-destructive bg-destructive/10 border-destructive/30 [&>svg]:text-destructive",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Alert({ className, variant, ...props }) {
  return (
    <div data-slot="alert" role="alert" className={cn(alertVariants({ variant }), className)} {...props} />
  );
}

function AlertDescription({ className, ...props }) {
  return (
    <div
      data-slot="alert-description"
      className={cn("text-sm leading-relaxed col-start-2", className)}
      {...props}
    />
  );
}

export { Alert, AlertDescription };
