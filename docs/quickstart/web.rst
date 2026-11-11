Website
=======

No install needed: everything runs at
`paperpush.onrender.com <https://paperpush.onrender.com>`_.

Venue-specific metadata collection, venue-tailored checking, and submission portal filling for a supported venue
----------------------------------------------------------------------------------------------------------------

1. Visit `paperpush.onrender.com <https://paperpush.onrender.com>`_.
2. Select the venue (or upload a ``.sub`` file).

   - For general manuscript checks, select **General**.

3. Upload the manuscript files and fill out the metadata fields.
4. Click the blue **Validate** button to validate the files and metadata
   against the author guidelines.
5. Click the blue **Share** button to share the editable form with co-authors
   (or download and share the ``.sub`` file).
6. If available, click **Fill out submission portal automatically** and follow
   the instructions.

The ``.sub`` file you download from the website works with the
:doc:`command line <cli>` and the :doc:`AI agent skills <ai>`, and a ``.sub``
made with either can be uploaded back to the website. To open one in a local
copy of the web app instead, run ``paperpush web VENUE.sub.txt`` (see
:doc:`../commands/web`).
