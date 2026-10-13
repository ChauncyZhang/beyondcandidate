import test from "node:test";
import assert from "node:assert/strict";
import { createOfferController } from "./offerController.js";

const metadata = { filename: "formal.pdf", size_bytes: 1234, content_type: "application/pdf", sha256: "abc" };
function raw(changes = {}) {
  return { id: "test-offer", version: 4, status: "ready_to_send", candidate_response_deadline: "2099-01-01T00:00:00Z", can_view_sensitive_content: true, allowed_actions: { manage_attachment: true, send: true }, attachment: null, ...changes };
}
const pdf = () => new File(["%PDF-1.7 test"], "formal.pdf", { type: "application/pdf" });

test("attachment upload, replacement, removal and reload preserve metadata and current send version", async () => {
  let stored = raw();
  const calls = [];
  const controller = createOfferController({ idempotencyKey: () => "test-key", client: { async request(path, options = {}) {
    calls.push({ path, options });
    if (options.method) {
      assert.equal(options.ifMatch, `"${stored.version}"`);
      assert.equal(options.idempotencyKey, "test-key");
      stored = { ...stored, version: stored.version + 1, attachment: options.method === "DELETE" ? null : metadata };
    }
    return { data: stored };
  } } });
  let offer = await controller.getOffer("test-offer");
  assert.equal(offer.attachment, null);
  offer = await controller.uploadAttachment(offer, pdf());
  assert.equal(calls.at(-1).path, "/api/v1/offers/test-offer/attachment");
  assert.equal(calls.at(-1).options.body.get("file").name, "formal.pdf");
  assert.deepEqual(offer.attachment, { filename: "formal.pdf", sizeBytes: 1234, contentType: "application/pdf", sha256: "abc" });
  assert.deepEqual((await controller.getOffer(offer.id)).attachment, offer.attachment);
  offer = await controller.uploadAttachment(offer, pdf());
  assert.equal(offer.version, 6);
  offer = await controller.removeAttachment(offer);
  assert.equal(offer.version, 7);
  assert.equal(offer.attachment, null);
  await controller.send(offer);
  assert.equal(calls.at(-1).options.ifMatch, '"7"');
});

test("attachment metadata is redacted and permissions fail closed", async () => {
  for (const changes of [{ can_view_sensitive_content: false, attachment: metadata }, { allowed_actions: { send: true } }, { status: "sent" }, { send_queued: true }, { deadline_expired: true }, { candidate_response_deadline: "2000-01-01T00:00:00Z" }]) {
    let calls = 0;
    const controller = createOfferController({ client: { async request() { calls++; return { data: raw(changes) }; } } });
    const offer = await controller.getOffer("test-offer");
    if (changes.can_view_sensitive_content === false) assert.equal(offer.attachment, null);
    await assert.rejects(controller.uploadAttachment(offer, pdf()), { code: "offer_attachment_locked" });
    await assert.rejects(controller.removeAttachment(offer), { code: "offer_attachment_locked" });
    assert.equal(calls, 1);
  }
});

test("invalid and oversized files never reach API; exact limit and absent browser MIME are accepted", async () => {
  let calls = 0;
  const controller = createOfferController({ client: { async request(path, options = {}) {
    calls++;
    if (options.body) {
      assert.equal(options.body.get("file").type, "application/pdf");
      assert.equal(options.body.get("file").name, "a.PDF");
    }
    return { data: raw() };
  } } });
  const offer = await controller.getOffer("test-offer");
  for (const file of [new File(["bad"], "a.txt"), new File(["bad"], "a.pdf", { type: "text/plain" }), new File([], "a.pdf")]) {
    await assert.rejects(controller.uploadAttachment(offer, file), { code: "offer_attachment_invalid" });
  }
  await assert.rejects(controller.uploadAttachment(offer, new File([new Uint8Array(10 * 1024 * 1024 + 1)], "a.pdf")), { code: "offer_attachment_too_large" });
  assert.equal(calls, 1);
  await controller.uploadAttachment(offer, new File([new Uint8Array(10 * 1024 * 1024)], "a.PDF"));
  assert.equal(calls, 2);
});

test("pending attachment blocks send, delete and duplicate upload; pending send blocks attachment", async () => {
  let resolve;
  let calls = 0;
  const controller = createOfferController({ client: { request(path, options = {}) {
    calls++;
    return options.method ? new Promise((done) => { resolve = done; }) : Promise.resolve({ data: raw() });
  } } });
  const offer = await controller.getOffer("test-offer");
  for (const start of [() => controller.uploadAttachment(offer, pdf()), () => controller.removeAttachment(offer), () => controller.send(offer)]) {
    const pending = start();
    await assert.rejects(controller.send(offer), { code: "offer_attachment_busy" });
    await assert.rejects(controller.removeAttachment(offer), { code: "offer_attachment_busy" });
    await assert.rejects(controller.uploadAttachment(offer, pdf()), { code: "offer_attachment_busy" });
    resolve({ data: raw({ version: 5 }) });
    await pending;
  }
  assert.equal(calls, 4);
});

test("attachment conflicts refresh the offer without automatic replay and server errors propagate", async () => {
  for (const code of ["resource_version_conflict", "offer_attachment_invalid", "offer_attachment_too_large", "offer_attachment_scan_unavailable", "offer_attachment_unsafe", "offer_attachment_locked"]) {
    const paths = [];
    const controller = createOfferController({ client: { async request(path, options = {}) {
      paths.push(path);
      if (options.method) throw Object.assign(new Error("safe"), { code });
      return { data: raw({ version: paths.length > 1 ? 8 : 4, attachment: metadata }) };
    } } });
    const offer = await controller.getOffer("test-offer");
    await assert.rejects(controller.uploadAttachment(offer, pdf()), (error) => {
      assert.equal(error.code, code);
      if (code === "resource_version_conflict") assert.equal(error.latestOffer.version, 8);
      return true;
    });
    assert.equal(paths.length, code === "resource_version_conflict" ? 3 : 2);
  }
});
