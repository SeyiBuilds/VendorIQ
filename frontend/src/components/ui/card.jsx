import * as React from "react";
import { cn } from "@/lib/utils";

function Card({ className, ...props }) {
  return (
    <div
      data-slot="card"
      className={cn(
        "bg-card text-card-foreground flex flex-col gap-4 rounded-xl border shadow-[0_1px_2px_rgba(0,0,0,0.3),0_8px_24px_rgba(0,0,0,0.28)] py-5",
        className,
      )}
      {...props}
    />
  );
}

function CardHeader({ className, ...props }) {
  return (
    <div
      data-slot="card-header"
      className={cn("flex flex-col gap-1.5 px-5", className)}
      {...props}
    />
  );
}

function CardTitle({ className, ...props }) {
  return (
    <h2
      data-slot="card-title"
      className={cn("font-bold leading-none text-base", className)}
      {...props}
    />
  );
}

function CardDescription({ className, ...props }) {
  return (
    <p
      data-slot="card-description"
      className={cn("text-muted-foreground text-sm leading-relaxed", className)}
      {...props}
    />
  );
}

function CardAction({ className, ...props }) {
  return (
    <div
      data-slot="card-action"
      className={cn("ml-auto", className)}
      {...props}
    />
  );
}

function CardContent({ className, ...props }) {
  return (
    <div data-slot="card-content" className={cn("px-5", className)} {...props} />
  );
}

function CardFooter({ className, ...props }) {
  return (
    <div
      data-slot="card-footer"
      className={cn("flex items-center px-5", className)}
      {...props}
    />
  );
}

export { Card, CardHeader, CardFooter, CardTitle, CardAction, CardDescription, CardContent };
