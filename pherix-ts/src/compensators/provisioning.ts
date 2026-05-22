/**
 * Infrastructure provisioning compensators.
 * Mirror of pherix/compensators/provisioning.py.
 *
 *   createResource → deleteResource   (spin up infra → tear it down)
 *   scaleUp        → scaleDown        (raise capacity → lower it back)
 *
 * `createResource → deleteResource` is a clean left-inverse keyed by
 * `resourceId`. `scaleUp → scaleDown` is the catalog's example of an inverse
 * that is only correct when the caller carries the *before-value in the args*:
 * scaling is relative, so `scaleUp` takes both `fromReplicas` and `toReplicas`
 * and `scaleDown` restores `fromReplicas`. The before-state lives in the args
 * precisely because the engine fires the compensator with the action's args and
 * nothing else.
 */

import { tool, type ToolWrapper } from "../tools.js";

export interface ProvisioningClient {
  createResource(resourceId: string, kind: string, spec: unknown): unknown;
  deleteResource(resourceId: string): unknown;
  scale(target: string, replicas: number): unknown;
}

export interface ResourceArgs extends Record<string, unknown> {
  resourceId: string;
  kind: string;
  spec: unknown;
}

/** Register `createResource` and its left-inverse `deleteResource`. Reverses by
 *  `resourceId`. */
export function registerCreateDeleteResource(
  client: ProvisioningClient,
  resource = "provisioning",
): {
  createResource: ToolWrapper<ResourceArgs, unknown>;
  deleteResource: ToolWrapper<ResourceArgs, unknown>;
} {
  const deleteResource = tool<ResourceArgs>(
    resource,
    (args: ResourceArgs) => client.deleteResource(args.resourceId),
    { name: "deleteResource", reversible: false, injectsHandle: false },
  );

  const createResource = tool<ResourceArgs>(
    resource,
    (args: ResourceArgs) => client.createResource(args.resourceId, args.kind, args.spec),
    {
      name: "createResource",
      reversible: false,
      injectsHandle: false,
      compensator: "deleteResource",
    },
  );

  return { createResource, deleteResource };
}

export interface ScaleArgs extends Record<string, unknown> {
  target: string;
  fromReplicas: number;
  toReplicas: number;
}

/** Register `scaleUp` and its left-inverse `scaleDown`. The action carries both
 *  endpoints so the compensator can restore the exact prior capacity. Reverses
 *  by `(target, fromReplicas)`. */
export function registerScaleUpDown(
  client: ProvisioningClient,
  resource = "provisioning",
): { scaleUp: ToolWrapper<ScaleArgs, unknown>; scaleDown: ToolWrapper<ScaleArgs, unknown> } {
  const scaleDown = tool<ScaleArgs>(
    resource,
    // Restore the before-value carried in the action's args.
    (args: ScaleArgs) => client.scale(args.target, args.fromReplicas),
    { name: "scaleDown", reversible: false, injectsHandle: false },
  );

  const scaleUp = tool<ScaleArgs>(
    resource,
    (args: ScaleArgs) => client.scale(args.target, args.toReplicas),
    { name: "scaleUp", reversible: false, injectsHandle: false, compensator: "scaleDown" },
  );

  return { scaleUp, scaleDown };
}
