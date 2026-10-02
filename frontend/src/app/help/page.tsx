import { getSession } from "@/lib/server";

export const metadata = { title: "Help · Invoice Intelligence" };

type Topic = { id: string; title: string };

const TOPICS: Topic[] = [
  { id: "start", title: "Getting started" },
  { id: "add-invoices", title: "Adding invoices" },
  { id: "needs-a-look", title: "When an invoice needs a look" },
  { id: "spending", title: "Spending" },
  { id: "match", title: "Matching items" },
  { id: "alerts", title: "Price alerts" },
  { id: "savings", title: "Savings" },
  { id: "products", title: "Products" },
  { id: "export", title: "Sending invoices to your accountant" },
  { id: "phone", title: "Using it on your phone" },
  { id: "emails", title: "Emails we send you" },
  { id: "account", title: "Your account and password" },
  { id: "words", title: "What the labels mean" },
];

const ADMIN_TOPIC: Topic = { id: "admin", title: "For admins" };

function topic(id: string): Topic {
  return TOPICS.find((t) => t.id === id)!;
}

/** One page that answers "how do I…?", with a link to where each thing is
 *  done. Open to everyone, signed in or not: someone who can't sign in needs
 *  it most. */
export default async function HelpPage() {
  const session = await getSession();
  const user = session?.user;
  const admin = !!user?.is_operator;
  const topics = admin ? [...TOPICS, ADMIN_TOPIC] : TOPICS;
  const inboxes = (user?.locations ?? []).filter((l) => l.inbox_address);

  return (
    <div className="max-w-3xl">
      <h1 className="page-title">Help</h1>
      <p className="mt-0.5 text-sm text-gray-500">
        How to get your invoices in, and what to do with what comes out.
        {!user && (
          <>
            {" "}
            <a href="/login" className="link">
              Sign in
            </a>{" "}
            to use the links below.
          </>
        )}
      </p>

      <nav aria-label="Help topics" className="card mt-4 p-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500">On this page</h2>
        <ul className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          {topics.map((t) => (
            <li key={t.id}>
              <a href={`#${t.id}`} className="link">
                {t.title}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <div className="mt-6 space-y-8 text-sm leading-relaxed text-gray-700">
        <Section topic={topic("start")}>
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              <strong>Add your invoices</strong> on the <Go href="/invoices">Invoices</Go> page. We read the items and
              prices for you.
            </li>
            <li>
              <strong>Match items</strong> we weren&rsquo;t sure about on the <Go href="/review">Match items</Go> page.
              It takes a second each.
            </li>
            <li>
              <strong>Check</strong> <Go href="/insights">Price alerts</Go> for prices that went up, and{" "}
              <Go href="/negotiation">Savings</Go> for what to raise with your rep.
            </li>
          </ol>
        </Section>

        <Section topic={topic("add-invoices")}>
          <p>Four ways, all on the <Go href="/invoices">Invoices</Go> page:</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>
              <strong>Upload a PDF.</strong> Press <em>Upload invoice</em> and choose the file. You can choose several;
              each becomes its own invoice.
            </li>
            <li>
              <strong>Take photos</strong> of a paper invoice. On a phone, press <em>Take photo</em>. For an invoice
              with several pages, add a photo of each page, then press <em>Upload invoice</em>.
            </li>
            <li>
              <strong>Email it.</strong> Each location has its own invoice email address. Send or forward invoices
              there, or ask your distributor to send them there directly. You can email photos of a paper invoice
              too, a photo per page. Zipped files and forwarded emails with the invoice inside work as well. Send
              only invoices there: any photo emailed to it is read as an invoice.
              {inboxes.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {inboxes.map((l) => (
                    <li key={l.id}>
                      {l.name}:{" "}
                      <span className="font-medium text-gray-900 [overflow-wrap:anywhere]">{l.inbox_address}</span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
            <li>
              <strong>Type it in,</strong> when you have no file or photo of it. Press <em>Type one in</em>, choose
              the distributor and the date, then add each item and the total as the invoice prints them. Type each
              line total and the total as printed, not worked out: with no picture to compare against, the numbers
              adding up is how a slip in the typing gets caught. Its prices count once you press{" "}
              <em>Confirm invoice</em>.
            </li>
          </ul>
          <p className="mt-2">
            Reading an invoice takes about a minute. The page updates by itself when it&rsquo;s done.
          </p>
          <p className="mt-2">
            Added the same invoice twice? We won&rsquo;t take the same file again, and a second copy (a rescan, or one
            that was also emailed) is held so it isn&rsquo;t counted twice. Open it and press <em>Delete it</em>, or{" "}
            <em>It&rsquo;s a different invoice</em> if it isn&rsquo;t a copy. Statements and price lists are held the
            same way: they aren&rsquo;t invoices, so nothing on them is used.
          </p>
          <p className="mt-2">
            Several invoices in one file (a week&rsquo;s stack scanned together, or photos of two invoices in one
            email) are separated: each becomes its own invoice. A long till receipt can be one tall photo.
          </p>
          <p className="mt-2">
            An invoice made out to a different restaurant is held too, in case it was added to the wrong location.
            Press <em>Delete it</em> and add it where it belongs, or <em>It&rsquo;s ours</em>.
          </p>
          <p className="mt-2">
            PDFs with a password can&rsquo;t be read. Open the file, save a copy without the password, and add that. A PDF
            can have up to 100 pages; split a longer one.
          </p>
        </Section>

        <Section topic={topic("needs-a-look")}>
          <p>
            We check that every invoice adds up. If it doesn&rsquo;t, it&rsquo;s marked <Label>Needs a look</Label>{" "}
            and its prices aren&rsquo;t used until someone fixes it. Open the invoice from the{" "}
            <Go href="/invoices">Invoices</Go> page:
          </p>
          <ol className="mt-2 list-decimal space-y-1 pl-5">
            <li>Compare the highlighted items with the picture of the invoice.</li>
            <li>Fix anything that was misread, then press <em>Save and check</em>.</li>
            <li>
              When everything adds up, press <em>Confirm invoice</em>.
            </li>
          </ol>
          <p className="mt-2">
            If it asks you to choose the distributor and yours isn&rsquo;t in the list (a local produce or seafood
            company, say), choose <em>+ Add a distributor</em> and type their name. Their next invoices are recognized
            by name.
          </p>
          <p className="mt-2">
            If an invoice is marked <Label>Couldn&rsquo;t read</Label>, type its items in from the picture with{" "}
            <em>+ Add item</em>. Start typing an item you&rsquo;ve bought before and we&rsquo;ll fill it in.
          </p>
        </Section>

        <Section topic={topic("spending")}>
          <p>
            <Go href="/spending">Spending</Go> shows what you spend each month, before tax, and where it goes: by
            category (meat, produce, dairy and so on) and by distributor, with how each changed since the month before.
            Click a month to see its breakdown. Invoices that need a look aren&rsquo;t counted until they&rsquo;re
            fixed.
          </p>
        </Section>

        <Section topic={topic("match")}>
          <p>
            Every invoice item is matched to a product, like &ldquo;Mozzarella, shredded&rdquo;, so its price can be
            followed over time and across distributors. We match most items ourselves. The ones we&rsquo;re unsure
            about wait on the <Go href="/review">Match items</Go> page.
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>
              If our guess is right, press <em>Enter</em> (or <em>That&rsquo;s right</em>).
            </li>
            <li>If it&rsquo;s wrong, type the right product&rsquo;s name and pick it from the list.</li>
            <li>
              Not sure? Press <em>Skip for now</em>. It&rsquo;ll be there next time.
            </li>
            <li>
              A fee, deposit or discount rather than something you bought? Press <em>Not a product</em>. (We spot most
              of these ourselves; they show as <Label>Fee or charge</Label> on the invoice and in{" "}
              <Go href="/spending">Spending</Go>.)
            </li>
          </ul>
          <p className="mt-2">
            Each item appears once, however many invoices it&rsquo;s on (&ldquo;On 7 invoices&rdquo;): matching it
            matches it on all of them, and on later invoices too. When lots of our guesses are very likely right,{" "}
            <em>Accept all</em> takes them in one go and leaves the doubtful ones for you.
          </p>
          <p className="mt-2">
            Some invoices don&rsquo;t print a pack size, so we can&rsquo;t work out a price per pound or gallon. Where
            that happens, type the pack size in (like 4/5 LB) and press <em>Save</em>. We remember it for that item,
            fill it in on its earlier invoices, and use it on every new one. You can also add it from the invoice
            page (<em>Add pack size to track its price</em>).
          </p>
          <p className="mt-2">
            Spot an item matched to the wrong product (on an invoice, or in a product&rsquo;s history)? Press{" "}
            <em>Wrong product?</em> next to it and it goes back to <Go href="/review">Match items</Go>.
          </p>
        </Section>

        <Section topic={topic("alerts")}>
          <p>
            <Go href="/insights">Price alerts</Go> lists products you&rsquo;re now paying noticeably more for than you
            used to. Where we know what similar businesses pay, you&rsquo;ll see how your price compares. Open a
            product to see every price you&rsquo;ve paid for it. Once you&rsquo;ve dealt with one (called your rep, or
            accepted it), press <em>Dealt with it</em> to take it off the list; we&rsquo;ll tell you again if the price
            goes up more.
          </p>
          <p className="mt-2">
            If the same product costs less at another distributor, the alert lists it under{" "}
            <em>Costs less elsewhere</em>, with the price, what you&rsquo;d save in a year, a link to that
            distributor, and a next step:
          </p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li>
              <strong>Ask your distributor to match.</strong> Usually the first move, and free. The link opens{" "}
              <Go href="/negotiation">Savings</Go>, which has the numbers to show your rep.
            </li>
            <li>
              <strong>Move it.</strong> Only when the other distributor already delivers to you, the saving is worth
              it, and the product is a small part of your order.
            </li>
            <li>
              <strong>Stay.</strong> The saving is too small, or not worth opening a new account for.
            </li>
          </ul>
          <p className="mt-2">
            Buying mostly from one distributor usually earns better prices, so a lower price elsewhere isn&rsquo;t
            always a saving. Press <em>What switching would involve</em> to see why. We can&rsquo;t see your
            contract, so ask about rebates and minimums before you split an order.
          </p>
        </Section>

        <Section topic={topic("savings")}>
          <p>
            <Go href="/negotiation">Savings</Go> lists products you could be paying less for, the price to ask for,
            and what that would save in a year. Press <em>Print</em> and take it to your rep.
          </p>
          <p className="mt-2">The buttons at the top change what the target price is based on:</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li>
              <strong>What others pay</strong>: a low price that similar businesses near you pay.
            </li>
            <li>
              <strong>What you used to pay</strong>: your own usual price. Works for every product.
            </li>
            <li>
              <strong>Best available</strong>: what others pay where we know it, otherwise what you used to pay.
            </li>
          </ul>
        </Section>

        <Section topic={topic("products")}>
          <p>
            Search <Go href="/skus">Products</Go> to see everything you&rsquo;ve paid for one product, from every
            distributor, with the invoice each price came from.
          </p>
        </Section>

        <Section topic={topic("export")}>
          <p>
            On the <Go href="/invoices">Invoices</Go> page, press <em>Export</em>. Choose invoices or every item on
            them, the dates, and a distributor if you want just one. <strong>Excel</strong> is best for reading;{" "}
            <strong>CSV</strong> is for importing into accounting software.
          </p>
        </Section>

        <Section topic={topic("phone")}>
          <p>
            Put the app on your phone&rsquo;s home screen and it opens like any other app, straight to your invoices,
            ready to take a photo.
          </p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>
              <strong>iPhone:</strong> open this site in Safari, tap <em>Share</em>, then <em>Add to Home Screen</em>.
            </li>
            <li>
              <strong>Android:</strong> open it in Chrome, tap the menu (&#8942;), then <em>Add to Home screen</em> or{" "}
              <em>Install app</em>.
            </li>
          </ul>
        </Section>

        <Section topic={topic("emails")}>
          <ul className="list-disc space-y-1 pl-5">
            <li>
              <strong>Price increases, as they happen</strong>: an email the same day a price jumps, about twenty minutes after
              your last invoice arrives. Increases found in old invoices you add later aren&rsquo;t emailed; they&rsquo;re on
              Price alerts.
            </li>
            <li>
              <strong>Weekly summary</strong>: every Monday, what happened that week and what needs doing.
            </li>
          </ul>
          <p className="mt-2">
            Turn either one off on <Go href="/account/password">your account</Go> page, or with the link at the bottom
            of any of those emails.
          </p>
        </Section>

        <Section topic={topic("account")}>
          <ul className="list-disc space-y-1 pl-5">
            <li>
              Change your password on <Go href="/account/password">your account</Go> page (press your name at the top).
            </li>
            <li>
              Forgot it? Use <Go href="/forgot-password">Forgot your password?</Go> on the sign-in page and we&rsquo;ll
              email you a link.
            </li>
            <li>If you have several locations, switch between them with the menu at the top of every page.</li>
            <li>
              Need access to another location? Ask whoever set up your account.
            </li>
          </ul>
        </Section>

        <Section topic={topic("words")}>
          <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-[max-content_1fr]">
            <Term label="Reading">We&rsquo;re reading the invoice. Give it a minute.</Term>
            <Term label="Ready">Read, and everything adds up. Its prices are being used.</Term>
            <Term label="Needs a look">Some numbers don&rsquo;t add up. Open it to fix them.</Term>
            <Term label="Couldn't read">We couldn&rsquo;t read it. Type its items in from the picture.</Term>
            <Term label="Confirmed">Someone checked it. Its prices are being used.</Term>
            <Term label="Matched / Confirmed / Fixed">
              An item that&rsquo;s been matched to a product, by us, or by a person.
            </Term>
            <Term label="To match">An item waiting on the <Go href="/review">Match items</Go> page.</Term>
          </dl>
        </Section>

        {admin && (
          <Section topic={ADMIN_TOPIC}>
            <ul className="list-disc space-y-1 pl-5">
              <li>
                <strong>Add a restaurant</strong> on <Go href="/accounts">Businesses</Go>. It gets its own invoice
                email address. Put locations with the same owner into one business.
              </li>
              <li>
                <strong>Give someone access</strong> on <Go href="/users">People</Go>: add them, tick their locations,
                and give them the temporary password shown. They choose their own when they first sign in.
              </li>
              <li>
                <strong>See who changed what</strong>, anywhere, in the <Go href="/audit">Change log</Go>.
              </li>
            </ul>
          </Section>
        )}
      </div>

      <p className="mt-10 border-t border-gray-200 pt-4 text-sm text-gray-500">
        Still stuck? Ask whoever set up your account.{" "}
        <a href="#start" className="link">
          Back to top
        </a>
      </p>
    </div>
  );
}

function Section({ topic, children }: { topic: Topic; children: React.ReactNode }) {
  return (
    <section id={topic.id} className="scroll-mt-24">
      <h2 className="section-title mb-2 text-gray-900">{topic.title}</h2>
      {children}
    </section>
  );
}

function Go({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} className="link">
      {children}
    </a>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <span className="badge bg-gray-100 text-gray-800">{children}</span>;
}

function Term({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="font-semibold text-gray-900">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}
